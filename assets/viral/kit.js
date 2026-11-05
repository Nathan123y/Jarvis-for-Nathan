// The 3D kit behind the viral videos (worker/viral.py). A video's scene module gets `K` and builds its
// world from these pieces; the kit runs the clock, the camera rig, the effects and the on-screen text.
// Everything is deterministic: frames are rendered one by one at a fixed step, and Math.random is seeded.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { SMAAPass } from 'three/addons/postprocessing/SMAAPass.js';
import { Sky } from 'three/addons/objects/Sky.js';
import { ImprovedNoise } from 'three/addons/math/ImprovedNoise.js';
import { loadHumans, humanKit } from './humans.js';
import { loadDinos, dinoKit } from './dinos.js';

const W = 1080, H = 1920;

// ── randomness, easing, timing ───────────────────────────────────────────────
function mulberry(seed) {
  let a = seed >>> 0;
  return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
const clamp01 = x => Math.max(0, Math.min(1, x));
const ease = {
  linear: x => clamp01(x),
  in: x => (x = clamp01(x), x * x * x),
  out: x => (x = clamp01(x), 1 - Math.pow(1 - x, 3)),
  inOut: x => (x = clamp01(x), x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
  expoIn: x => (x = clamp01(x), x === 0 ? 0 : Math.pow(2, 10 * x - 10)),
  expoOut: x => (x = clamp01(x), x === 1 ? 1 : 1 - Math.pow(2, -10 * x)),
  back: x => (x = clamp01(x), 1 + 2.70158 * Math.pow(x - 1, 3) + 1.70158 * Math.pow(x - 1, 2)),
};
const v3 = a => a instanceof THREE.Vector3 ? a.clone() : new THREE.Vector3(a[0] || 0, a[1] || 0, a[2] || 0);

// ── procedural textures ──────────────────────────────────────────────────────
function canvasTex(w, h, draw, repeat = 1) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(repeat, repeat);
  t.anisotropy = 8;
  return t;
}
function speckle(base, vary, n = 9000, size = 3) {
  return (g, w, h) => {
    const c = new THREE.Color(base);
    g.fillStyle = '#' + c.getHexString(); g.fillRect(0, 0, w, h);
    for (let i = 0; i < n; i++) {
      const k = c.clone().offsetHSL((Math.random() - .5) * vary * .2, (Math.random() - .5) * vary * .4, (Math.random() - .5) * vary);
      g.fillStyle = '#' + k.getHexString();
      const s = 1 + Math.random() * size;
      g.fillRect(Math.random() * w, Math.random() * h, s, s);
    }
  };
}
function glowTex(inner = 'rgba(255,255,255,1)', mid = 'rgba(255,255,255,.35)') {
  return canvasTex(256, 256, (g) => {
    const r = g.createRadialGradient(128, 128, 0, 128, 128, 128);
    r.addColorStop(0, inner); r.addColorStop(.25, mid); r.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = r; g.fillRect(0, 0, 256, 256);
  });
}
function softTex() {
  return canvasTex(128, 128, (g) => {
    const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    r.addColorStop(0, 'rgba(255,255,255,.9)'); r.addColorStop(.5, 'rgba(255,255,255,.4)'); r.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  });
}

// ── the kit ──────────────────────────────────────────────────────────────────
export async function boot(data, sceneUrl) {
  await Promise.all([loadHumans(), loadDinos()]);
  Math.random = mulberry(data.seed || 7);
  const noise = new ImprovedNoise();
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, logarithmicDepthBuffer: true });
  renderer.setPixelRatio(1); renderer.setSize(W, H);
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  document.body.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(62, W / H, 0.05, 200000);
  camera.position.set(0, 1.6, 6);
  const rt = new THREE.WebGLRenderTarget(W, H, { type: THREE.HalfFloatType });
  // no MSAA here: with bloom it tears whole blocks out of the frame on Apple GPUs; SMAA smooths the edges instead
  const composer = new EffectComposer(renderer, rt);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(W, H), 0.55, 0.5, 0.82);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());
  composer.addPass(new SMAAPass(W, H));

  const updaters = [];          // (t, dt) => void, run by the kit every frame
  const shakes = [];
  let handheld = 0.6, camKeys = null;
  const beats = data.beats || [];
  const GLOW = glowTex(), SOFT = softTex();

  const K = {
    THREE, scene, camera, renderer, bloom, ease, noise, W, H,
    beats, duration: data.duration,
    rand: (a = 0, b = 1) => a + Math.random() * (b - a),
    /** progress 0..1 of t between a and b (seconds), optionally eased */
    p: (t, a, b, e = 'linear') => ease[e]((t - a) / Math.max(1e-6, b - a)),
    /** start / end seconds of beat i (negative i counts from the end) */
    at: i => beats[(i + beats.length) % beats.length].start,
    end: i => beats[(i + beats.length) % beats.length].end,
    lerp: (a, b, x) => a + (b - a) * x,
    v3,
    onUpdate: fn => updaters.push(fn),
    mat: (color, o = {}) => new THREE.MeshStandardMaterial(Object.assign({ color, roughness: .85, metalness: 0 }, o)),
    glowMat: (color, intensity = 3) => new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(intensity), toneMapped: false }),
    add: (obj, pos, shadows = true) => {
      if (pos) obj.position.copy(v3(pos));
      if (shadows) obj.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
      scene.add(obj); return obj;
    },
  };

  // ── environment: sky, light, fog ───────────────────────────────────────────
  const ENV = {
    day:      { elev: 42, az: 20, turb: 5, ray: 1.4, mie: .004, sun: 2.6, sunC: 0xfff4e6, hemi: [0xbfd8ff, 0x5a6b3a, 1.1], fog: [0xbcd3ea, 120, 900], exp: .55 },
    golden:   { elev: 9,  az: 35, turb: 8, ray: 2.4, mie: .006, sun: 2.6, sunC: 0xffc58a, hemi: [0xffd6b0, 0x5a5040, 1.3], fog: [0xf0c9a0, 90, 700], exp: .5 },
    dusk:     { elev: 5, az: 50, turb: 8, ray: 3.0, mie: .007, sun: 2.0, sunC: 0xffa070, hemi: [0xb6aaff, 0x55553e, 1.6], fog: [0x7a6f9c, 70, 560], exp: .72 },
    overcast: { bg: 0x9aa3ab, sun: .7, sunC: 0xffffff, hemi: [0xdfe6ee, 0x5d6360, 1.5], fog: [0x9aa3ab, 30, 260], exp: .9 },
    night:    { bg: 0x0a1024, stars: true, sun: .8, sunC: 0x9fb4ff, hemi: [0x40558f, 0x15181f, .9], fog: [0x070b18, 50, 420], exp: 1.0 },
    space:    { bg: 0x000000, stars: true, sun: 3.2, sunC: 0xffffff, hemi: [0x223355, 0x000000, .08], exp: 1.0 },
    underwater: { bg: 0x0a4a5e, sun: 1.2, sunC: 0xbff6ff, hemi: [0x6fd6e8, 0x062a35, 1.0], fog: [0x0b5468, 4, 70], exp: .9 },
    hell:     { bg: 0x1a0503, sun: 1.6, sunC: 0xff5a2a, hemi: [0xff6a3a, 0x200805, .7], fog: [0x3a0c05, 20, 240], exp: .9 },
  };
  K.env = (name = 'day', o = {}) => {
    const e = Object.assign({}, ENV[name] || ENV.day, o);
    const out = {};
    if (e.elev !== undefined) {
      const sky = new Sky(); sky.scale.setScalar(150000);
      const u = sky.material.uniforms;
      u.turbidity.value = e.turb; u.rayleigh.value = e.ray; u.mieCoefficient.value = e.mie; u.mieDirectionalG.value = .8;
      const sunDir = new THREE.Vector3().setFromSphericalCoords(1, THREE.MathUtils.degToRad(90 - e.elev), THREE.MathUtils.degToRad(e.az));
      u.sunPosition.value.copy(sunDir);
      scene.add(sky); out.sky = sky; out.sunDir = sunDir;
    } else {
      scene.background = new THREE.Color(e.bg);
      out.sunDir = new THREE.Vector3(-.4, .8, .3).normalize();
    }
    renderer.toneMappingExposure = e.exp;
    const hemi = new THREE.HemisphereLight(e.hemi[0], e.hemi[1], e.hemi[2]); scene.add(hemi);
    const sun = new THREE.DirectionalLight(e.sunC, e.sun);
    sun.position.copy(out.sunDir.clone().multiplyScalar(120)); sun.castShadow = true;
    sun.shadow.mapSize.set(4096, 4096);
    const s = sun.shadow.camera; s.left = -60; s.right = 60; s.top = 60; s.bottom = -60; s.near = 1; s.far = 400;
    sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02;
    scene.add(sun); scene.add(sun.target);
    if (e.fog) scene.fog = new THREE.Fog(e.fog[0], e.fog[1], e.fog[2]);
    if (e.stars) out.stars = K.stars();
    Object.assign(out, { sun, hemi, name });
    K.envInfo = out;
    return out;
  };

  K.stars = (n = 5000, r = 90000) => {
    const g = new THREE.BufferGeometry(), pos = [], col = [];
    for (let i = 0; i < n; i++) {
      const v = new THREE.Vector3().randomDirection().multiplyScalar(r);
      pos.push(v.x, v.y, v.z);
      const c = new THREE.Color().setHSL(.55 + Math.random() * .12, .4, .7 + Math.random() * .3);
      col.push(c.r, c.g, c.b);
    }
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    const pts = new THREE.Points(g, new THREE.PointsMaterial({ size: 2.2, sizeAttenuation: false, vertexColors: true, fog: false, depthWrite: false }));
    scene.add(pts); return pts;
  };

  // ── ground and grass ───────────────────────────────────────────────────────
  const GROUND = { grass: [0x4a7d30, .35], sand: [0xd9c08a, .18], dirt: [0x6b4a2e, .3], concrete: [0x8a8a86, .15],
                   snow: [0xeef3f8, .06], asphalt: [0x2c2d30, .15], rock: [0x6d655c, .3], mars: [0xb5522c, .25], moon: [0x9a9894, .2] };
  K.ground = (type = 'grass', size = 3000) => {
    const [c, v] = GROUND[type] || GROUND.grass;
    const tex = canvasTex(512, 512, speckle(c, v), size / 6);
    const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
    m.rotation.x = -Math.PI / 2; m.receiveShadow = true; scene.add(m);
    return m;
  };
  /** dense blades of grass that sway, in a disc of `radius` around `center` */
  K.grass = (center = [0, 0, 0], radius = 14, count = 70000, color = 0x4f8a32) => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute([-.035, 0, 0, .035, 0, 0, 0, .32, 0], 3));
    geo.setAttribute('normal', new THREE.Float32BufferAttribute([0, 0, 1, 0, 0, 1, 0, 0, 1], 3));
    const mat = new THREE.MeshLambertMaterial({ side: THREE.DoubleSide });
    const uTime = { value: 0 };
    mat.onBeforeCompile = (sh) => {
      sh.uniforms.uTime = uTime;
      sh.vertexShader = 'uniform float uTime;\n' + sh.vertexShader.replace('#include <begin_vertex>',
        `#include <begin_vertex>
         vec4 ip = instanceMatrix[3];
         float h = position.y / 0.32;
         transformed.x += sin(uTime * 1.7 + ip.x * .45 + ip.z * .3) * .07 * h * h;
         transformed.z += cos(uTime * 1.3 + ip.z * .4) * .04 * h * h;`);
    };
    const inst = new THREE.InstancedMesh(geo, mat, count);
    const base = new THREE.Color(color), o = new THREE.Object3D(), c0 = v3(center);
    for (let i = 0; i < count; i++) {
      const a = Math.random() * Math.PI * 2, r = Math.sqrt(Math.random()) * radius;
      o.position.set(c0.x + Math.cos(a) * r, c0.y, c0.z + Math.sin(a) * r);
      o.rotation.set((Math.random() - .5) * .4, Math.random() * Math.PI, 0);
      const s = .6 + Math.random() * .9; o.scale.set(s, s * (.7 + Math.random() * .7), s);
      o.updateMatrix(); inst.setMatrixAt(i, o.matrix);
      inst.setColorAt(i, base.clone().offsetHSL((Math.random() - .5) * .05, 0, (Math.random() - .5) * .14));
    }
    inst.receiveShadow = true; scene.add(inst);
    updaters.push(t => { uTime.value = t; });
    return inst;
  };

  // ── props ──────────────────────────────────────────────────────────────────
  K.tree = (pos = [0, 0, 0], s = 1, leaf = 0x3f7a2c) => {
    const g = new THREE.Group();
    const trunk = new THREE.Mesh(new THREE.CylinderGeometry(.18 * s, .28 * s, 3 * s, 8), K.mat(0x5a3e2b));
    trunk.position.y = 1.5 * s; g.add(trunk);
    for (let i = 0; i < 6; i++) {
      const r = (1 + Math.random() * .7) * s;
      const m = new THREE.Mesh(new THREE.IcosahedronGeometry(r, 1),
        K.mat(new THREE.Color(leaf).offsetHSL((Math.random() - .5) * .04, 0, (Math.random() - .5) * .1), { flatShading: true }));
      m.position.set((Math.random() - .5) * 1.8 * s, (3.2 + Math.random() * 1.6) * s, (Math.random() - .5) * 1.8 * s);
      g.add(m);
    }
    return K.add(g, pos);
  };
  K.house = (pos = [0, 0, 0], o = {}) => {
    const { w = 10, d = 8, h = 6, color = 0xd8c9a0, roof = 0x3b3f4a, rot = 0, lit = false, stories = 2 } = o;
    const g = new THREE.Group();
    const siding = canvasTex(256, 256, (c, cw, ch) => {
      c.fillStyle = '#' + new THREE.Color(color).getHexString(); c.fillRect(0, 0, cw, ch);
      c.strokeStyle = 'rgba(0,0,0,.13)'; c.lineWidth = 2;
      for (let y = 0; y < ch; y += 16) { c.beginPath(); c.moveTo(0, y); c.lineTo(cw, y); c.stroke(); }
    });
    siding.repeat.set(w / 3, h / 3);
    const body = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshStandardMaterial({ map: siding, roughness: .9 }));
    body.position.y = h / 2; g.add(body);
    const roofG = new THREE.CylinderGeometry(0.01, d * .62, 2.6, 4, 1); roofG.rotateY(Math.PI / 4);
    const rf = new THREE.Mesh(roofG, K.mat(roof, { flatShading: true }));
    rf.scale.set(w / d * 1.02, 1, 1.02); rf.position.y = h + 1.3; g.add(rf);
    const glass = lit ? new THREE.MeshStandardMaterial({ color: 0x332211, emissive: 0xffc070, emissiveIntensity: .85 })
                      : K.mat(0x1d2533, { roughness: .2, metalness: .3 });
    const frame = K.mat(0xf4f1ea);
    const rows = stories, cols = Math.max(2, Math.round(w / 2.6));
    for (let r = 0; r < rows; r++) for (let cI = 0; cI < cols; cI++) {
      const x = -w / 2 + (cI + .5) * (w / cols), y = (r + .55) * (h / rows);
      const f = new THREE.Mesh(new THREE.BoxGeometry(1.3, 1.6, .08), frame); f.position.set(x, y, d / 2 + .02); g.add(f);
      const gl = new THREE.Mesh(new THREE.BoxGeometry(1.08, 1.38, .1), glass); gl.position.set(x, y, d / 2 + .04); g.add(gl);
    }
    g.rotation.y = rot;
    return K.add(g, pos);
  };
  K.fence = (a, b, h = 1.9, color = 0x6b4630) => {
    a = v3(a); b = v3(b);
    const len = a.distanceTo(b), n = Math.ceil(len / .16);
    const inst = new THREE.InstancedMesh(new THREE.BoxGeometry(.15, h, .04), K.mat(color), n);
    const o = new THREE.Object3D(), ang = Math.atan2(b.x - a.x, b.z - a.z);
    for (let i = 0; i < n; i++) {
      o.position.lerpVectors(a, b, (i + .5) / n); o.position.y = h / 2 + (Math.random() - .5) * .03;
      o.rotation.set(0, ang + Math.PI / 2, 0); o.updateMatrix(); inst.setMatrixAt(i, o.matrix);
      inst.setColorAt(i, new THREE.Color(color).offsetHSL(0, 0, (Math.random() - .5) * .06));
    }
    inst.castShadow = inst.receiveShadow = true; scene.add(inst); return inst;
  };
  K.chair = (pos, color = 0x2a9d9a, rot = 0) => {
    const g = new THREE.Group(), m = K.mat(color);
    const seat = new THREE.Mesh(new THREE.BoxGeometry(.8, .08, .8), m); seat.position.y = .45; g.add(seat);
    const back = new THREE.Mesh(new THREE.BoxGeometry(.8, .9, .08), m); back.position.set(0, .85, -.4); back.rotation.x = -.25; g.add(back);
    for (const [x, z] of [[-.35, -.35], [.35, -.35], [-.35, .35], [.35, .35]]) {
      const l = new THREE.Mesh(new THREE.BoxGeometry(.07, .45, .07), m); l.position.set(x, .22, z); g.add(l);
    }
    g.rotation.y = rot; return K.add(g, pos);
  };
  K.car = (pos, color = 0xb02a2a, rot = 0) => {
    const g = new THREE.Group();
    const body = new THREE.Mesh(new THREE.BoxGeometry(1.9, .7, 4.3), K.mat(color, { roughness: .35, metalness: .5 })); body.position.y = .65; g.add(body);
    const cab = new THREE.Mesh(new THREE.BoxGeometry(1.7, .6, 2.2), K.mat(0x1a2230, { roughness: .1, metalness: .6 })); cab.position.set(0, 1.25, -.2); g.add(cab);
    for (const [x, z] of [[-.95, 1.3], [.95, 1.3], [-.95, -1.3], [.95, -1.3]]) {
      const wh = new THREE.Mesh(new THREE.CylinderGeometry(.36, .36, .28, 16), K.mat(0x111111)); wh.rotation.z = Math.PI / 2; wh.position.set(x, .36, z); g.add(wh);
    }
    g.rotation.y = rot; return K.add(g, pos);
  };
  humanKit(K, updaters);   // K.person, K.crowd: real people (humans.js)
  dinoKit(K);              // K.dino (dinos.js)
  /** pigeons pecking around [x,z]; flock.scatter(t0) sends them flying */
  K.birds = (center = [0, 0, 0], count = 10, radius = 3) => {
    const c = v3(center), list = [];
    for (let i = 0; i < count; i++) {
      const g = new THREE.Group();
      const body = new THREE.Mesh(new THREE.SphereGeometry(.12, 8, 6), K.mat(0x6f7480, { flatShading: true })); body.scale.set(1, .8, 1.5); g.add(body);
      const head = new THREE.Mesh(new THREE.SphereGeometry(.06, 8, 6), K.mat(0x4d5260, { flatShading: true })); head.position.set(0, .1, .14); g.add(head);
      g.position.set(c.x + (Math.random() - .5) * radius * 2, .1, c.z + (Math.random() - .5) * radius * 2); g.rotation.y = Math.random() * 6.28;
      g.userData.ph = Math.random() * 10; g.userData.head = head; K.add(g); list.push(g);
    }
    let scatterAt = 1e9;
    updaters.push((t) => { for (const g of list) {
      const a = t - scatterAt;
      if (a > 0) { g.position.y = .1 + a * a * 3 + a * 2; g.position.x += Math.sin(g.userData.ph) * .08; g.position.z += Math.cos(g.userData.ph) * .08; }
      else g.userData.head.position.y = .1 - Math.max(0, Math.sin(t * 5 + g.userData.ph)) * .08;
    } });
    list.scatter = (t0) => { scatterAt = t0; };
    return list;
  };
  /** a first-person forearm in the lower right of the frame, holding o.item: 'coffee' | 'phone' | 'none' */
  K.hand = (o = {}) => {
    const { item = 'coffee', skin = 0xd9a07a, sleeve = 0x3a6ea5 } = o;
    const g = new THREE.Group();
    const skinM = K.mat(skin, { roughness: .55 }), sleeveM = K.mat(sleeve, { roughness: .9 });
    const seg = (a, b, r, m) => {            // a capsule from point a to point b
      const A = v3(a), B = v3(b), msh = new THREE.Mesh(new THREE.CapsuleGeometry(r, Math.max(.001, A.distanceTo(B)), 8, 18), m);
      msh.position.copy(A).lerp(B, .5); msh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), B.clone().sub(A).normalize()); g.add(msh); return msh;
    };
    if (item === 'coffee') {
      const cup = new THREE.Mesh(new THREE.CylinderGeometry(.05, .04, .15, 40), K.mat(0xf3efe6, { roughness: .5 })); cup.position.set(0, .09, -.06); g.add(cup);
      const lid = new THREE.Mesh(new THREE.CylinderGeometry(.053, .053, .022, 40), K.mat(0x1b1b1b, { roughness: .4 })); lid.position.set(0, .175, -.06); g.add(lid);
      const rim = new THREE.Mesh(new THREE.TorusGeometry(.051, .006, 8, 40), K.mat(0x1b1b1b, { roughness: .4 })); rim.rotation.x = Math.PI / 2; rim.position.set(0, .186, -.06); g.add(rim);
      const sl = new THREE.Mesh(new THREE.CylinderGeometry(.0515, .0465, .06, 40), K.mat(0x9a6a3a, { roughness: .95 })); sl.position.set(0, .085, -.06); g.add(sl);
    } else if (item === 'phone') {
      const ph = new THREE.Mesh(new THREE.BoxGeometry(.075, .15, .01), K.mat(0x111111, { roughness: .3 })); ph.position.set(0, .09, -.07); ph.rotation.x = -.5; g.add(ph);
      const sc = new THREE.Mesh(new THREE.PlaneGeometry(.065, .135), K.glowMat(0x7fb0ff, 1.2)); sc.position.set(0, .09, -.064); sc.rotation.x = -.5; g.add(sc);
    }
    // the hand grips the cup from the right: palm on its side, fingers round the back, thumb across the front
    const palm = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16), skinM); palm.scale.set(.024, .05, .042); palm.position.set(.062, .085, -.055); g.add(palm);
    for (let i = 0; i < 4; i++) {
      const y = .118 - i * .021, r = i === 3 ? .0095 : .011;
      seg([.06, y, -.085], [.02, y - .003, -.112], r, skinM); seg([.02, y - .003, -.112], [-.03, y - .006, -.1], r * .95, skinM);
    }
    seg([.066, .1, -.022], [.03, .118, -.004], .0125, skinM);
    seg([.068, .055, -.04], [.085, .0, .02], .03, skinM);                // wrist
    seg([.085, .0, .02], [.14, -.2, .32], .045, sleeveM);                 // sleeve
    const cuff = new THREE.Mesh(new THREE.TorusGeometry(.045, .008, 8, 24), sleeveM); cuff.position.set(.088, -.01, .035); cuff.lookAt(g.localToWorld(new THREE.Vector3(.14, -.2, .32))); g.add(cuff);
    g.position.set(.055, -.15, -.34); g.rotation.set(.15, -.3, 0); g.scale.setScalar(.55);
    g.traverse(m => { if (m.isMesh) { m.castShadow = false; m.material.fog = false; } });
    scene.add(camera); camera.add(g);
    g.bob = (t, amt = 1) => { g.position.y = -.15 + Math.sin(t * 6) * .006 * amt; g.position.x = .055 + Math.cos(t * 3) * .004 * amt; };
    return g;
  };
  K.box = (pos, size = [1, 1, 1], color = 0x888888, o = {}) => K.add(new THREE.Mesh(new THREE.BoxGeometry(...size), K.mat(color, o)), pos);
  K.ball = (pos, r = .5, color = 0xffffff, o = {}) => K.add(new THREE.Mesh(new THREE.SphereGeometry(r, 32, 20), K.mat(color, o)), pos);

  // ── glow, light, beams ─────────────────────────────────────────────────────
  /** a glowing point: sprite halo + bright core + a point light. g.setPower(x) scales all three */
  K.glow = (pos, o = {}) => {
    const { color = 0xffffff, size = 2, core = .12, light = 30, range = 40 } = o;
    const g = new THREE.Group();
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, fog: false }));
    sp.scale.setScalar(size); g.add(sp);
    const c = new THREE.Mesh(new THREE.SphereGeometry(core, 20, 14), K.glowMat(color, 6)); g.add(c);
    const pl = light ? new THREE.PointLight(color, light, range, 2) : null; if (pl) g.add(pl);
    g.setPower = (x) => { sp.scale.setScalar(size * x); sp.material.opacity = Math.min(1, x); c.scale.setScalar(Math.max(.001, x)); if (pl) pl.intensity = light * x; };
    g.sprite = sp; g.core = c; g.light = pl;
    return K.add(g, pos, false);
  };
  K.beam = (from, to, o = {}) => {
    const { color = 0x88ccff, width = .2, intensity = 4 } = o;
    const a = v3(from), b = v3(to), len = a.distanceTo(b);
    const m = new THREE.Mesh(new THREE.CylinderGeometry(width, width, len, 12, 1, true), new THREE.MeshBasicMaterial({
      color: new THREE.Color(color).multiplyScalar(intensity), transparent: true, opacity: .9, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false }));
    m.position.lerpVectors(a, b, .5); m.lookAt(b); m.rotateX(Math.PI / 2);
    return K.add(m, null, false);
  };
  /** a streaking falling object (meteor-like): a glowing head plus a long additive tail */
  K.streak = (o = {}) => {
    const { color = 0xdfeeff, size = 3, tail = 60 } = o;
    const g = new THREE.Group();
    const head = K.glow([0, 0, 0], { color, size, light: 60, range: 120 }); scene.remove(head); g.add(head);
    const tg = new THREE.CylinderGeometry(size * .22, 0.01, tail, 16, 1, true); tg.translate(0, tail / 2, 0);
    const tm = new THREE.Mesh(tg, new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(2.5), transparent: true, opacity: .55,
      blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false }));
    g.add(tm); g.head = head; g.tail = tm;
    /** place it along from→to at progress x (0..1); the tail points back up the path */
    g.fly = (from, to, x) => { const a = v3(from), b = v3(to); g.position.lerpVectors(a, b, x); const up = a.clone().sub(b).normalize();
      g.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), up); g.visible = x > 0 && x < 1; };
    return K.add(g, null, false);
  };

  // ── particles ──────────────────────────────────────────────────────────────
  /** sparks / embers / dust: points flung from `pos` at t0. o.dir biases direction, o.spread 0..1 */
  K.sparks = (pos, o = {}) => {
    const { t0 = 0, count = 400, speed = 12, color = 0xffb060, life = 1.6, gravity = -9.8, size = .12, drag = .4,
            dir = [0, 1, 0], spread = 1, additive = true } = o;
    const g = new THREE.BufferGeometry(), P = new Float32Array(count * 3), V = [], L = [];
    const d = v3(dir).normalize(), p0 = v3(pos);
    for (let i = 0; i < count; i++) {
      const r = new THREE.Vector3().randomDirection().lerp(d, 1 - spread).normalize().multiplyScalar(speed * (.3 + Math.random() * .9));
      V.push(r); L.push(life * (.4 + Math.random() * .8)); P.set([p0.x, p0.y, p0.z], i * 3);
    }
    g.setAttribute('position', new THREE.BufferAttribute(P, 3));
    const mat = new THREE.PointsMaterial({ color: new THREE.Color(color).multiplyScalar(additive ? 3 : 1), size, map: SOFT, transparent: true,
      depthWrite: false, blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending, toneMapped: !additive });
    const pts = new THREE.Points(g, mat); pts.visible = false; pts.frustumCulled = false; scene.add(pts);
    updaters.push((t) => {
      const age = t - t0; pts.visible = age >= 0 && age < life * 1.3; if (!pts.visible) return;
      for (let i = 0; i < count; i++) {
        const k = Math.max(0, Math.min(age, L[i])), damp = (1 - Math.exp(-drag * k)) / Math.max(drag, 1e-4);
        P[i * 3] = p0.x + V[i].x * damp; P[i * 3 + 1] = Math.max(0.02, p0.y + V[i].y * damp + .5 * gravity * k * k); P[i * 3 + 2] = p0.z + V[i].z * damp;
      }
      g.attributes.position.needsUpdate = true; mat.opacity = 1 - ease.in(age / (life * 1.3));
    });
    return pts;
  };
  /** chunks of dirt / rubble thrown from `pos` at t0; they bounce once and settle */
  K.debris = (pos, o = {}) => {
    const { t0 = 0, count = 120, speed = 10, color = 0x5a3d26, size = .25, gravity = -12, spread = .8, dir = [0, 1, 0] } = o;
    const inst = new THREE.InstancedMesh(new THREE.DodecahedronGeometry(size, 0), K.mat(color, { flatShading: true }), count);
    const p0 = v3(pos), d = v3(dir).normalize(), V = [], S = [], R = [], o3 = new THREE.Object3D();
    for (let i = 0; i < count; i++) {
      V.push(new THREE.Vector3().randomDirection().lerp(d, 1 - spread).normalize().multiplyScalar(speed * (.3 + Math.random())));
      S.push(.4 + Math.random() * 1.2); R.push(new THREE.Vector3(Math.random(), Math.random(), Math.random()).multiplyScalar(8));
      inst.setColorAt(i, new THREE.Color(color).offsetHSL(0, 0, (Math.random() - .5) * .15));
    }
    inst.castShadow = true; inst.visible = false; inst.frustumCulled = false; scene.add(inst);
    updaters.push((t) => {
      const age = t - t0; inst.visible = age >= 0; if (!inst.visible) return;
      for (let i = 0; i < count; i++) {
        const v = V[i]; let y = p0.y + v.y * age + .5 * gravity * age * age;
        const tLand = (-v.y - Math.sqrt(Math.max(0, v.y * v.y - 2 * gravity * p0.y))) / gravity;
        const a = Math.min(age, tLand > 0 ? tLand : age);
        y = Math.max(size * S[i] * .5, p0.y + v.y * a + .5 * gravity * a * a);
        o3.position.set(p0.x + v.x * a, y, p0.z + v.z * a);
        o3.rotation.set(R[i].x * a, R[i].y * a, R[i].z * a); o3.scale.setScalar(S[i]); o3.updateMatrix(); inst.setMatrixAt(i, o3.matrix);
      }
      inst.instanceMatrix.needsUpdate = true;
    });
    return inst;
  };
  /** a column of smoke from t0 to t1: soft sprites that rise, grow and fade */
  K.smoke = (pos, o = {}) => {
    const { t0 = 0, t1 = 1e9, rate = 14, rise = 2.5, size = 3, color = 0x3a3634, life = 4, spread = 1, opacity = .55 } = o;
    const n = Math.min(400, Math.ceil(rate * life) + 4), sprites = [], p0 = v3(pos);
    for (let i = 0; i < n; i++) {
      const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: SOFT, color, transparent: true, depthWrite: false, opacity: 0 }));
      s.userData = { off: new THREE.Vector3((Math.random() - .5) * spread, 0, (Math.random() - .5) * spread), drift: (Math.random() - .5) * .8, rot: Math.random() };
      scene.add(s); sprites.push(s);
    }
    updaters.push((t) => {
      for (let i = 0; i < n; i++) {
        const s = sprites[i], period = n / rate, born = t0 + (i / rate), cycles = Math.floor((t - born) / period);
        const b = born + Math.max(0, cycles) * period, age = t - b;
        if (t < born || b > t1 || age > life) { s.visible = false; continue; }
        const k = age / life; s.visible = true;
        s.position.set(p0.x + s.userData.off.x + s.userData.drift * age, p0.y + rise * age, p0.z + s.userData.off.z);
        s.scale.setScalar(size * (.4 + k * 1.6)); s.material.opacity = opacity * Math.sin(Math.PI * k); s.material.rotation = s.userData.rot + age * .2;
      }
    });
    return sprites;
  };
  /** flickering fire at `pos` from t0 to t1 */
  K.fire = (pos, o = {}) => {
    const { t0 = 0, t1 = 1e9, size = 1.5, color = 0xff7a20 } = o;
    const sm = K.smoke(pos, { t0, t1, rate: 28, rise: size * 1.4, size: size * .9, color, life: .9, spread: size * .6, opacity: .9 });
    sm.forEach(s => { s.material.blending = THREE.AdditiveBlending; s.material.color.multiplyScalar(2.2); s.material.toneMapped = false; });
    const pl = new THREE.PointLight(color, 0, size * 14, 2); pl.position.copy(v3(pos)).add(new THREE.Vector3(0, size * .6, 0)); scene.add(pl);
    updaters.push((t) => { pl.intensity = (t >= t0 && t <= t1) ? size * 25 * (0.8 + .2 * Math.sin(t * 23) * Math.sin(t * 7.1)) : 0; });
    return pl;
  };
  /** the full blast: flash, fireball, shock ring, sparks, debris, smoke and camera shake */
  K.explosion = (pos, o = {}) => {
    const { t0 = 0, size = 4, color = 0xff8a30, debris = true, smoke = true, shake = 1 } = o;
    const p0 = v3(pos);
    const fb = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 20), new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(4), transparent: true, toneMapped: false, depthWrite: false }));
    fb.position.copy(p0); fb.visible = false; scene.add(fb);
    // a flat ring on the ground (a torus seen edge-on from eye level reads as a solid slab)
    const ring = new THREE.Mesh(new THREE.RingGeometry(.86, 1, 96), new THREE.MeshBasicMaterial({ color: new THREE.Color(0xfff0d0).multiplyScalar(2),
      transparent: true, toneMapped: false, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2; ring.position.copy(p0).setY(p0.y + .2); ring.visible = false; scene.add(ring);
    const pl = new THREE.PointLight(color, 0, size * 40, 2); pl.position.copy(p0).add(new THREE.Vector3(0, size, 0)); scene.add(pl);
    K.sparks(p0, { t0, count: 600, speed: size * 4, color, life: 2.2, size: .18 * size / 4 });
    if (debris) K.debris(p0, { t0, count: 160, speed: size * 3, size: .18 * size / 4 + .08 });
    if (smoke) K.smoke(p0.clone().setY(p0.y + size * .5), { t0: t0 + .3, t1: t0 + 6, rate: 10, rise: size * .7, size: size * 2.2, life: 6, spread: size });
    K.shake(t0, 1.6, .35 * shake * size / 4);
    updaters.push((t) => {
      const a = t - t0;
      fb.visible = a >= 0 && a < 1.2; ring.visible = a >= 0 && a < 1.4;
      if (fb.visible) { fb.scale.setScalar(size * ease.expoOut(a / .5)); fb.material.opacity = 1 - ease.in(a / 1.2); }
      if (ring.visible) { ring.scale.setScalar(size * 4 * ease.out(a / 1.4)); ring.material.opacity = .7 * (1 - a / 1.4); }
      pl.intensity = a >= 0 ? size * 400 * Math.exp(-a * 3) : 0;
    });
    return { fireball: fb, ring, light: pl };
  };

  // ── water, earth, planets ──────────────────────────────────────────────────
  K.ocean = (o = {}) => {
    const { size = 2000, color = 0x1d5f8a, waves = .6, seg = 160, y = 0 } = o;
    const g = new THREE.PlaneGeometry(size, size, seg, seg); g.rotateX(-Math.PI / 2);
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color, roughness: .18, metalness: .2, flatShading: true }));
    m.position.y = y; m.receiveShadow = true; scene.add(m);
    const P = g.attributes.position, base = P.array.slice();
    updaters.push((t) => {
      for (let i = 0; i < P.count; i++) {
        const x = base[i * 3], z = base[i * 3 + 2];
        P.array[i * 3 + 1] = waves * (Math.sin(x * .05 + t * 1.2) * .6 + Math.sin(z * .07 - t * .9) * .5 + noise.noise(x * .03, z * .03, t * .2) * 1.2);
      }
      P.needsUpdate = true; g.computeVertexNormals();
    });
    return m;
  };
  function planetTex(kind, seed) {
    return canvasTex(1024, 512, (g, w, h) => {
      const img = g.createImageData(w, h);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        const lon = x / w * Math.PI * 2, lat = (y / h - .5) * Math.PI;
        const nx = Math.cos(lat) * Math.cos(lon), ny = Math.sin(lat), nz = Math.cos(lat) * Math.sin(lon);
        let n = 0, amp = 1, f = 1.6;
        for (let o = 0; o < 5; o++) { n += amp * noise.noise(nx * f + seed, ny * f, nz * f); amp *= .5; f *= 2; }
        let c;
        if (kind === 'earth') {
          const ice = Math.abs(ny) > .86 + n * .1;
          c = ice ? [235, 240, 245] : n > .06 ? (n > .32 ? [120, 105, 80] : [62 + n * 80, 110 - n * 40, 52]) : [18 + n * 40, 52 + n * 60, 120 + n * 80];
        } else if (kind === 'clouds') {
          const v = Math.max(0, n * 2.4 - .1); c = [255, 255, 255, Math.min(255, v * 255)];
        } else if (kind === 'gas') {
          const b = Math.sin(ny * 14 + n * 3); c = [196 + b * 30, 150 + b * 30, 110 + b * 20];
        } else if (kind === 'mars') {
          c = [170 + n * 70, 80 + n * 40, 45 + n * 20];
        } else { // moon / rock
          const v = 140 + n * 90; c = [v, v, v * .97];
        }
        const i = (y * w + x) * 4; img.data[i] = c[0]; img.data[i + 1] = c[1]; img.data[i + 2] = c[2]; img.data[i + 3] = c[3] === undefined ? 255 : c[3];
      }
      g.putImageData(img, 0, 0);
    });
  }
  function atmosphere(r, color, power = 3) {
    const m = new THREE.ShaderMaterial({
      uniforms: { c: { value: new THREE.Color(color) }, p: { value: power } },
      vertexShader: 'varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }',
      fragmentShader: 'uniform vec3 c; uniform float p; varying vec3 vN; varying vec3 vV; void main(){ float f = pow(1.-abs(dot(vN,vV)), p); gl_FragColor = vec4(c*f*2.5, f); }',
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.FrontSide });
    return new THREE.Mesh(new THREE.SphereGeometry(r * 1.035, 64, 48), m);
  }
  /** a planet. kind: earth | mars | moon | gas. earth gets clouds + a blue atmosphere. It spins slowly. */
  K.planet = (pos = [0, 0, 0], o = {}) => {
    const { r = 100, kind = 'earth', spin = .02 } = o;
    const g = new THREE.Group();
    const m = new THREE.Mesh(new THREE.SphereGeometry(r, 96, 64), new THREE.MeshStandardMaterial({ map: planetTex(kind, Math.random() * 10), roughness: .9 }));
    g.add(m); g.surface = m;
    if (kind === 'earth') {
      const cl = new THREE.Mesh(new THREE.SphereGeometry(r * 1.012, 96, 64), new THREE.MeshStandardMaterial({ map: planetTex('clouds', 3.3), transparent: true, depthWrite: false }));
      g.add(cl); g.clouds = cl; g.add(atmosphere(r, 0x5aa0ff));
      updaters.push((t) => { cl.rotation.y = t * spin * 1.3; });
    } else if (kind === 'mars') g.add(atmosphere(r, 0xff9a6a, 4));
    updaters.push((t) => { m.rotation.y = t * spin; });
    return K.add(g, pos, false);
  };
  /** a star / sun: hot core + huge halo; bloom does the rest */
  K.sun = (pos, o = {}) => {
    const { r = 50, color = 0xffd9a0 } = o;
    const g = new THREE.Group();
    g.add(new THREE.Mesh(new THREE.SphereGeometry(r, 48, 32), K.glowMat(color, 5)));
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, depthWrite: false, fog: false }));
    sp.scale.setScalar(r * 7); g.add(sp);
    return K.add(g, pos, false);
  };
  /** a black hole: black sphere + glowing accretion disk */
  K.blackHole = (pos, o = {}) => {
    const { r = 2, color = 0xffa040 } = o;
    const g = new THREE.Group();
    g.add(new THREE.Mesh(new THREE.SphereGeometry(r, 48, 32), new THREE.MeshBasicMaterial({ color: 0x000000 })));
    const disk = new THREE.Mesh(new THREE.RingGeometry(r * 1.4, r * 4, 128, 1), new THREE.MeshBasicMaterial({
      map: canvasTex(512, 512, (c, w, h) => { const gr = c.createRadialGradient(w / 2, h / 2, w * .17, w / 2, h / 2, w / 2);
        gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(.3, 'rgba(255,200,120,.9)'); gr.addColorStop(1, 'rgba(255,90,20,0)');
        c.fillStyle = gr; c.fillRect(0, 0, w, h); }),
      color: new THREE.Color(color).multiplyScalar(3), side: THREE.DoubleSide, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false }));
    disk.rotation.x = -Math.PI / 2.3; g.add(disk); g.disk = disk;
    updaters.push((t) => { disk.rotation.z = t * .6; });
    return K.add(g, pos, false);
  };

  // ── ready-made sets: a full place in one call ──────────────────────────────
  K.sets = {
    /** a suburban backyard at dusk (or o.env): house behind a fence, neighbours, trees, grass, chairs */
    backyard(o = {}) {
      const env = K.env(o.env || 'dusk');
      K.ground('grass'); K.grass([0, 0, 0], 16, 80000);
      K.house([0, 0, -22], { w: 16, d: 9, h: 7, color: 0xd9c89c, lit: env.name === 'night' || env.name === 'dusk' });
      K.house([-21, 0, -24], { w: 12, color: 0xb9c7cf, rot: .15, lit: env.name === 'night' }); K.house([21, 0, -25], { w: 13, color: 0xc9b9a6, rot: -.12 });
      K.fence([-14, 0, -14], [14, 0, -14]); K.fence([-14, 0, -14], [-14, 0, 14]); K.fence([14, 0, -14], [14, 0, 14]);
      K.tree([-10, 0, -11], 1.2); K.tree([11, 0, -9], 1.4); K.tree([-26, 0, -6], 1.6); K.tree([25, 0, -4], 1.3);
      K.chair([5, 0, -6], 0x2a9d9a, -.4); K.chair([7, 0, -5], 0xe0b23a, -.7);
      return { env, center: new THREE.Vector3(0, 0, 0) };
    },
    /** a city street lined with towers; o.night lights the windows */
    city(o = {}) {
      const night = !!o.night, env = K.env(o.env || (night ? 'night' : 'golden'), night ? {} : { elev: 28 });
      K.ground('asphalt', 4000);
      const win = canvasTex(128, 256, (c, w, h) => { c.fillStyle = '#2a2f3a'; c.fillRect(0, 0, w, h);
        for (let y = 6; y < h; y += 16) for (let x = 6; x < w; x += 14) { c.fillStyle = Math.random() < (night ? .45 : .1) ? '#ffd890' : '#4a5a70'; c.fillRect(x, y, 8, 10); } });
      const bm = new THREE.MeshStandardMaterial({ map: win, emissiveMap: night ? win : null, emissive: night ? 0xffffff : 0x000000, emissiveIntensity: night ? .9 : 0, roughness: .6 });
      for (let i = 0; i < 70; i++) {
        const side = i % 2 ? 1 : -1, z = -Math.floor(i / 2) * 18 + 20, h = 18 + Math.random() * 90, w = 10 + Math.random() * 8;
        const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, 14), bm.clone()); m.material.map = win.clone(); m.material.map.repeat.set(w / 10, h / 20); m.material.map.needsUpdate = true;
        if (night) m.material.emissiveMap = m.material.map;
        K.add(m, [side * (16 + Math.random() * 4), h / 2, z]);
      }
      for (let i = 0; i < 14; i++) K.car([(i % 2 ? 3 : -3), 0, 20 - i * 22 + Math.random() * 8], [0xb02a2a, 0x2a5ab0, 0xdddddd, 0x222222, 0xe0b23a][i % 5], i % 2 ? Math.PI : 0);
      K.box([0, .02, -200], [.3, .04, 600], 0xf0e8c0, { emissive: 0x332a10 });
      return { env, center: new THREE.Vector3(0, 0, 0) };
    },
    /** open sea to the horizon */
    ocean(o = {}) { const env = K.env(o.env || 'day'); const sea = K.ocean({ waves: o.waves || .7 }); return { env, sea }; },
    /** a flat desert / beach / snowfield / mars plain: o.ground = sand | snow | mars | moon | rock */
    plain(o = {}) {
      const env = K.env(o.env || 'golden'); K.ground(o.ground || 'sand', 6000);
      for (let i = 0; i < 40; i++) { const r = 2 + Math.random() * 10; K.add(new THREE.Mesh(new THREE.DodecahedronGeometry(r, 1), K.mat(GROUND[o.ground || 'sand'][0], { flatShading: true })), [(Math.random() - .5) * 600, -r * .4, -60 - Math.random() * 600]); }
      return { env };
    },
    /** deep space with stars; add K.planet / K.sun yourself */
    space(o = {}) { const env = K.env('space', o); return { env }; },
    /** a busy daytime sidewalk (first-person street walk): sidewalk x 3..9 along -z, road x < 3, shopfronts at x 10,
     *  towers above, lamps, café tables, a hydrant, pigeons and a crowd walking both ways. Eye height 1.6 at x≈6. */
    street(o = {}) {
      const env = K.env(o.env || 'day', { elev: 55, az: 120, exp: .38, turb: 3 }); if (scene.fog) { scene.fog.near = 60; scene.fog.far = 420; }
      K.ground('asphalt', 3000);
      const walk = new THREE.Mesh(new THREE.BoxGeometry(6.4, .16, 400), new THREE.MeshStandardMaterial({ roughness: .95,
        map: canvasTex(256, 256, (c, w, h) => { c.fillStyle = '#c9c9c4'; c.fillRect(0, 0, w, h); c.strokeStyle = 'rgba(0,0,0,.18)'; c.lineWidth = 3;
          for (let y = 0; y <= h; y += 64) { c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke(); } c.beginPath(); c.moveTo(w / 2, 0); c.lineTo(w / 2, h); c.stroke(); }, 1) }));
      walk.material.map.repeat.set(3, 200); K.add(walk, [6.2, .08, -150]);
      K.add(new THREE.Mesh(new THREE.BoxGeometry(6.4, .16, 400), K.mat(0xbdbdb8)), [-12.2, .08, -150]);
      const awn = [0xc0392b, 0x2e86c1, 0x27ae60, 0x8e44ad, 0xe67e22, 0x16a085, 0xd4ac0d];
      for (let i = 0; i < 26; i++) {
        const z = 20 - i * 14, h = 20 + Math.random() * 60, col = [0xb8a48a, 0x9c8f86, 0x7d8a96, 0xc9b79c, 0x8b6f5a][i % 5];
        K.add(new THREE.Mesh(new THREE.BoxGeometry(8, h, 13.6), K.mat(col)), [14, h / 2, z]);
        const glass = new THREE.Mesh(new THREE.BoxGeometry(.2, 3.2, 11), K.mat(0x26313d, { roughness: .1, metalness: .5 })); K.add(glass, [9.95, 1.9, z]);
        const band = new THREE.Mesh(new THREE.BoxGeometry(.3, .7, 12), K.mat(awn[i % awn.length])); K.add(band, [9.9, 3.9, z]);
        for (let f = 1; f < h / 3.5 - 1; f++) for (let k = -2; k <= 2; k++)
          K.add(new THREE.Mesh(new THREE.BoxGeometry(.1, 1.4, 1.6), K.mat(0x1d2533, { roughness: .2, metalness: .4 })), [9.95, 5 + f * 3.5, z + k * 2.5], false);
        const hb = 20 + Math.random() * 60; K.add(new THREE.Mesh(new THREE.BoxGeometry(8, hb, 13.6), K.mat(col)), [-20, hb / 2, z]);
      }
      for (let i = 0; i < 14; i++) {
        const z = 10 - i * 18;
        K.add(new THREE.Mesh(new THREE.CylinderGeometry(.07, .09, 6, 8), K.mat(0x2b2f33, { metalness: .6 })), [3.4, 3, z]);
        K.add(new THREE.Mesh(new THREE.BoxGeometry(1.2, .12, .3), K.mat(0x2b2f33)), [3.9, 6, z]);
        if (i % 2) { K.box([8.2, .55, z - 4], [.8, .06, .8], 0x222222); for (const dz of [-.7, .7]) K.chair([8.2, .1, z - 4 + dz], 0x222222, dz > 0 ? Math.PI : 0); }
      }
      K.add(new THREE.Mesh(new THREE.CylinderGeometry(.16, .2, .7, 10), K.mat(0xc0392b)), [3.7, .5, -9]);
      for (let i = 0; i < 12; i++) K.car([(i % 2 ? -3 : 0.5), 0, 15 - i * 25], [0xb02a2a, 0x2a5ab0, 0xdddddd, 0x222222, 0xe0b23a, 0xf1c40f][i % 6], i % 2 ? Math.PI : 0);
      const birds = K.birds([5.5, .16, -7], 9, 1.6);
      // two lanes of walkers either side of the camera's path down the middle (x≈6)
      const n = o.people === undefined ? 70 : o.people;
      const people = K.crowd([3.9, -120], [5.1, 15], Math.ceil(n / 2), { walk: true }).concat(K.crowd([7.1, -120], [8.9, 15], Math.floor(n / 2), { walk: true }));
      people.forEach(p => p.position.y = .16);
      return { env, birds, people };
    },
    /** an indoor basketball arena: court x -7.5..7.5, z -14..14 (hoops at z = ±13), packed stands, bright lights */
    court(o = {}) {
      const env = K.env('overcast', { bg: 0x101218, fog: [0x101218, 60, 220], hemi: [0xffffff, 0x5a4a3a, 1.2], sun: .9, exp: .95 }); scene.fog.color.set(0x101218);
      const floor = new THREE.Mesh(new THREE.PlaneGeometry(17, 30), new THREE.MeshStandardMaterial({ roughness: .35,
        map: canvasTex(512, 900, (c, w, h) => { c.fillStyle = '#c8955a'; c.fillRect(0, 0, w, h);
          for (let x = 0; x < w; x += 18) { c.fillStyle = `rgba(90,50,20,${.05 + Math.random() * .08})`; c.fillRect(x, 0, 2, h); }
          c.strokeStyle = '#fff'; c.lineWidth = 6; c.strokeRect(20, 20, w - 40, h - 40);
          c.beginPath(); c.moveTo(20, h / 2); c.lineTo(w - 20, h / 2); c.stroke(); c.beginPath(); c.arc(w / 2, h / 2, 60, 0, 6.3); c.stroke();
          for (const y of [20, h - 20]) { const d = y < h / 2 ? 1 : -1; c.strokeRect(w / 2 - 80, y, 160, d * 190);
            c.beginPath(); c.arc(w / 2, y + d * 50, 220, d > 0 ? 0 : Math.PI, d > 0 ? Math.PI : 2 * Math.PI); c.stroke(); } }) }));
      floor.rotation.x = -Math.PI / 2; floor.position.y = .02; K.add(floor);
      K.ground('concrete', 400);
      for (const z of [-13.2, 13.2]) {
        const d = Math.sign(z);
        K.add(new THREE.Mesh(new THREE.CylinderGeometry(.12, .12, 3.4, 8), K.mat(0x333333)), [0, 1.7, z + d * .9]);
        K.add(new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.05, .05), K.mat(0xffffff, { transparent: true, opacity: .55 })), [0, 3.4, z]);
        const rim = new THREE.Mesh(new THREE.TorusGeometry(.23, .02, 8, 24), K.mat(0xff5a1f)); rim.rotation.x = Math.PI / 2; K.add(rim, [0, 3.05, z - d * .3]);
      }
      const fans = [];
      for (const side of [-1, 1]) for (let r = 0; r < 8; r++) {
        const x = side * (10 + r * 1.3), y = .6 + r * .9;
        K.add(new THREE.Mesh(new THREE.BoxGeometry(1.3, .5, 34), K.mat(0x23262e)), [x, y - .4, 0]);
        if (o.fans !== false) fans.push(...K.crowd([x - .2, -16], [x + .2, 16], 26, { walk: false, face: [0, 0], s: .9 }).map(p => (p.position.y = y - .15, p)));
      }
      for (let i = -2; i <= 2; i++) { const l = new THREE.PointLight(0xfff4e0, 60, 40, 2); l.position.set(0, 14, i * 7); scene.add(l); }
      const ball = K.ball([0, .12, 0], .12, 0xd35400);
      fans.cheer = (on = true, t0 = 0) => K.cheer(on, t0);
      return { env, ball, fans };
    },
    /** a boxing ring under spotlights in a dark arena: ring floor y = 1.2, x and z in -3.5..3.5 */
    ring(o = {}) {
      const env = K.env('night', { stars: false, bg: 0x050608, fog: [0x050608, 20, 90], hemi: [0x8090b0, 0x101010, .5] });
      K.ground('concrete', 300);
      K.box([0, .6, 0], [8, 1.2, 8], 0x1d2a4a); K.add(new THREE.Mesh(new THREE.BoxGeometry(7.6, .05, 7.6), K.mat(0x9d978a)), [0, 1.22, 0]);
      for (const [x, z] of [[-3.7, -3.7], [3.7, -3.7], [-3.7, 3.7], [3.7, 3.7]]) K.add(new THREE.Mesh(new THREE.CylinderGeometry(.09, .09, 1.6, 8), K.mat(0xc0392b)), [x, 2, z]);
      for (const h of [1.7, 2.1, 2.5]) for (const [a, b] of [[[-3.7, h, -3.7], [3.7, h, -3.7]], [[-3.7, h, 3.7], [3.7, h, 3.7]], [[-3.7, h, -3.7], [-3.7, h, 3.7]], [[3.7, h, -3.7], [3.7, h, 3.7]]]) {
        const A = v3(a), B = v3(b), m = new THREE.Mesh(new THREE.CylinderGeometry(.03, .03, A.distanceTo(B), 6), K.mat(0xeeeeee));
        m.position.lerpVectors(A, B, .5); m.lookAt(B); m.rotateX(Math.PI / 2); K.add(m);
      }
      const spot = new THREE.SpotLight(0xfff2dd, 35, 40, .5, .6, 1.2); spot.position.set(0, 16, 0); spot.target.position.set(0, 1.2, 0); spot.castShadow = true; scene.add(spot, spot.target);
      const fans = K.crowd([-14, -14], [14, -8], 40, { walk: false, face: [0, 0] }).concat(K.crowd([-14, 8], [14, 14], 40, { walk: false, face: [0, 0] }));
      fans.cheer = (on = true, t0 = 0) => K.cheer(on, t0);
      return { env, fans, spot };
    },
    /** a small bedroom: walls, window, bed, desk, lamp. The room spans x -4..4, z -4..4, y 0..3 */
    room(o = {}) {
      const env = K.env(o.env || 'night'); scene.fog = null;
      const wall = K.mat(o.wall || 0x8d9ab0), floor = new THREE.MeshStandardMaterial({ map: canvasTex(256, 256, (c, w, h) => { c.fillStyle = '#7a5a3c'; c.fillRect(0, 0, w, h);
        for (let y = 0; y < h; y += 32) { c.fillStyle = `rgba(0,0,0,${.08 + Math.random() * .08})`; c.fillRect(0, y, w, 2); } }, 4), roughness: .7 });
      const f = new THREE.Mesh(new THREE.PlaneGeometry(8, 8), floor); f.rotation.x = -Math.PI / 2; K.add(f);
      for (const [p, r] of [[[0, 1.5, -4], 0], [[-4, 1.5, 0], Math.PI / 2], [[4, 1.5, 0], -Math.PI / 2]]) { const w = new THREE.Mesh(new THREE.PlaneGeometry(8, 3), wall); w.rotation.y = r; K.add(w, p); }
      const ceil = new THREE.Mesh(new THREE.PlaneGeometry(8, 8), K.mat(0xdedad2)); ceil.rotation.x = Math.PI / 2; K.add(ceil, [0, 3, 0]);
      K.box([-2.2, .3, -2.6], [2, .55, 2.6], 0xe8e4dc); K.box([-2.2, .7, -3.6], [2, .3, .6], 0xf6f3ee);
      K.box([2.6, .75, -3.3], [1.8, .08, .9], 0x6a4a30); K.box([2.6, .37, -3.3], [1.7, .74, .8], 0x5a3e28);
      const lamp = new THREE.PointLight(0xffc98a, 6, 12, 2); lamp.position.set(2.4, 1.6, -3.2); scene.add(lamp);
      const win = new THREE.Mesh(new THREE.PlaneGeometry(1.8, 1.3), new THREE.MeshBasicMaterial({ color: env.name === 'night' ? 0x1a2a55 : 0xbfe0ff }));
      K.add(win, [0, 1.8, -3.98], false);
      return { env, lamp, window: win };
    },
  };

  // ── football: ball, goals, tunnel, signs, sound rings, the stadium ─────────
  /** a low-poly match ball (white with black patches), radius r. ball.path([{t, pos, h?}]) moves it along keyframes
   *  (h = arc height for a lob/chip on the way to that key); it spins by the distance it travels. */
  K.soccerBall = (pos = [0, .11, 0], r = .11) => {
    const geo = new THREE.IcosahedronGeometry(1, 2).toNonIndexed();
    const ico = new THREE.IcosahedronGeometry(1, 0).attributes.position, tips = [];
    for (let i = 0; i < ico.count; i++) { const v = new THREE.Vector3().fromBufferAttribute(ico, i).normalize(); if (!tips.some(u => u.distanceTo(v) < 1e-3)) tips.push(v); }
    const pa = geo.attributes.position, col = [];
    for (let i = 0; i < pa.count; i += 3) {
      const c = new THREE.Vector3(); for (let k = 0; k < 3; k++) c.add(new THREE.Vector3().fromBufferAttribute(pa, i + k)); c.normalize();
      const dark = tips.some(u => u.angleTo(c) < .3);
      for (let k = 0; k < 3; k++) col.push(...(dark ? [.05, .05, .06] : [.96, .96, .96]));
    }
    geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3)); geo.scale(r, r, r);
    const m = K.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: .55 })), pos);
    let keys = null; const last = m.position.clone();
    m.path = (list) => { keys = list.map(k => ({ t: k.t, pos: v3(k.pos), h: k.h || 0, ease: k.ease })).sort((a, b) => a.t - b.t); return m; };
    updaters.push(t => {
      if (!keys || !keys.length) return;
      let i = 0; while (i < keys.length - 1 && keys[i + 1].t <= t) i++;
      const a = keys[i], b = keys[Math.min(i + 1, keys.length - 1)];
      const x = a === b ? 0 : (ease[b.ease || 'linear'] || ease.linear)((t - a.t) / Math.max(1e-6, b.t - a.t));
      m.position.lerpVectors(a.pos, b.pos, x); m.position.y = Math.max(r, m.position.y) + b.h * 4 * x * (1 - x) * (a === b ? 0 : 1);
      const d = m.position.clone().sub(last); d.y = 0; const dist = d.length();
      if (dist > 1e-5) m.rotateOnWorldAxis(new THREE.Vector3(d.z, 0, -d.x).normalize(), dist / r);
      last.copy(m.position);
    });
    return m;
  };
  /** a full-size goal (7.32 x 2.44 m, net 2 m deep) whose mouth faces `dir` (+1 = toward +z, -1 = toward -z), centred at x */
  K.goal = (pos = [0, 0, 0], dir = 1) => {
    const g = new THREE.Group(), post = K.mat(0xffffff, { roughness: .4 }), R = .06;
    for (const x of [-3.66, 3.66]) { const p = new THREE.Mesh(new THREE.CylinderGeometry(R, R, 2.44, 10), post); p.position.set(x, 1.22, 0); g.add(p); }
    const bar = new THREE.Mesh(new THREE.CylinderGeometry(R, R, 7.44, 10), post); bar.rotation.z = Math.PI / 2; bar.position.set(0, 2.44, 0); g.add(bar);
    const netTex = canvasTex(256, 256, (c, w, h) => { c.clearRect(0, 0, w, h); c.strokeStyle = 'rgba(255,255,255,.9)'; c.lineWidth = 3;
      for (let k = 0; k <= w; k += 32) { c.beginPath(); c.moveTo(k, 0); c.lineTo(k, h); c.stroke(); c.beginPath(); c.moveTo(0, k); c.lineTo(w, k); c.stroke(); } });
    const netMat = new THREE.MeshStandardMaterial({ map: netTex, transparent: true, alphaTest: .25, side: THREE.DoubleSide, roughness: 1 });
    const plane = (w, h, rep, setup) => { const t = netTex.clone(); t.repeat.set(rep[0], rep[1]); t.needsUpdate = true; const mm = netMat.clone(); mm.map = t;
      const p = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mm); setup(p); g.add(p); return p; };
    plane(7.32, 2.44, [14, 5], p => p.position.set(0, 1.22, -2));
    plane(7.32, 2, [14, 4], p => { p.rotation.x = Math.PI / 2; p.position.set(0, 2.44, -1); });
    for (const x of [-3.66, 3.66]) plane(2, 2.44, [4, 5], p => { p.rotation.y = Math.PI / 2; p.position.set(x, 1.22, -1); });
    g.rotation.y = dir > 0 ? 0 : Math.PI;
    return K.add(g, pos);
  };
  /** the players' tunnel: a striped arch `len` m long, its mouth at pos facing `rot` (radians about y; 0 = toward +z) */
  K.tunnel = (pos = [0, 0, 0], rot = 0, o = {}) => {
    const g = new THREE.Group(), len = o.len || 6, r = o.r || 2.2;
    const tex = canvasTex(256, 64, (c, w, h) => { for (let x = 0; x < w; x += 32) { c.fillStyle = (x / 32) % 2 ? (o.color || '#b3001b') : '#f4f4f4'; c.fillRect(x, 0, 32, h); } });
    tex.repeat.set(1, 1);
    const arch = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 32, 1, true, -Math.PI / 2, Math.PI), new THREE.MeshStandardMaterial({ map: tex, side: THREE.DoubleSide, roughness: .6 }));
    arch.rotation.x = Math.PI / 2; arch.position.set(0, 0, -len / 2); g.add(arch);
    const back = new THREE.Mesh(new THREE.CircleGeometry(r * .98, 32, 0, Math.PI), new THREE.MeshBasicMaterial({ color: 0xfff6dd })); back.position.set(0, 0, -len); g.add(back);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(r * 2, len), K.mat(0x2a2a2e)); floor.rotation.x = -Math.PI / 2; floor.position.set(0, .02, -len / 2); g.add(floor);
    const l = new THREE.PointLight(0xfff0d0, 20, 12, 2); l.position.set(0, r * .8, -len * .6); g.add(l);
    g.rotation.y = rot; g.mouth = v3(pos);
    return K.add(g, pos);
  };
  /** a flat text sign: banner, board, tifo or big screen. o: w, h (metres), bg, fg, size (text height share .1-.9),
   *  rot (yaw), tilt (pitch), glow (lit screen), border colour, font weight. sign.setText('...') redraws it. */
  K.sign = (text, pos = [0, 2, 0], o = {}) => {
    const w = o.w || 6, h = o.h || 1.6, cw = Math.min(4096, Math.round(w * 140)), ch = Math.max(16, Math.round(cw * h / w));
    const cv = document.createElement('canvas'); cv.width = cw; cv.height = ch; const c = cv.getContext('2d');
    const draw = (txt) => {
      c.fillStyle = o.bg || '#c8102e'; c.fillRect(0, 0, cw, ch);
      if (o.border) { const lw = Math.min(cw, ch) * .07; c.strokeStyle = o.border; c.lineWidth = lw; c.strokeRect(lw / 2, lw / 2, cw - lw, ch - lw); }
      const lines = String(txt).split('\n'); let size = ch * (o.size || .62) / lines.length;
      c.font = `${o.weight || 900} ${size}px ${SANS}`; const mw = Math.max(...lines.map(l => c.measureText(l).width));
      if (mw > cw * .9) { size *= cw * .9 / mw; c.font = `${o.weight || 900} ${size}px ${SANS}`; }
      c.fillStyle = o.fg || '#ffffff'; c.textAlign = 'center'; c.textBaseline = 'middle';
      lines.forEach((l, i) => c.fillText(l, cw / 2, ch * (i + .5) / lines.length + size * .05));
    };
    draw(text);
    const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
    const mat = o.glow ? new THREE.MeshBasicMaterial({ map: tex, toneMapped: false, side: THREE.DoubleSide })
                       : new THREE.MeshStandardMaterial({ map: tex, roughness: .8, emissive: 0xffffff, emissiveMap: tex, emissiveIntensity: .25, side: THREE.DoubleSide });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat);
    m.rotation.order = 'YXZ'; m.rotation.set(o.tilt || 0, o.rot || 0, 0);
    m.setText = (txt) => { draw(txt); tex.needsUpdate = true; return m; };
    return K.add(m, pos, false);
  };
  /** sound / sonar rings pulsing out of pos (or o.follow, an object): o.t0, o.t1, every (s between rings), life,
   *  size (final radius, m), color, opacity, flat (true = lying on the ground, false = facing the camera), y offset */
  K.rings = (pos, o = {}) => {
    const { t0 = 0, t1 = t0 + 2, every = .45, life = 1.3, size = 3, color = 0xffffff, opacity = .8, flat = true, y = .04 } = o;
    const geo = new THREE.RingGeometry(.9, 1, 72), pool = [], group = new THREE.Group();
    for (let i = 0; i < Math.ceil(life / every) + 2; i++) {
      const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
      m.visible = false; m.renderOrder = 5; group.add(m); pool.push(m);
    }
    scene.add(group); group.rings = pool;
    const _add = group.add.bind(group);
    group.add = (...o) => { if (o.length && o[0] && o[0].isObject3D) _add(...o); return group; };
    const base = pos ? v3(pos) : new THREE.Vector3(), tmp = new THREE.Vector3();
    updaters.push(t => {
      for (const m of pool) m.visible = false;
      if (t < t0 || t > t1 + life) return;
      const at = o.follow ? o.follow.getWorldPosition(tmp).add(v3(o.offset || [0, 0, 0])) : base;
      let k = 0;
      for (let s = t0 + Math.floor(Math.max(0, Math.min(t, t1) - t0) / every) * every; s > t - life && s >= t0 && k < pool.length; s -= every) {
        const a = (t - s) / life, m = pool[k++]; if (a < 0) continue;
        m.visible = true; m.position.copy(at);
        if (flat) { m.rotation.set(-Math.PI / 2, 0, 0); m.position.y = at.y + y; } else m.quaternion.copy(camera.quaternion);
        m.scale.setScalar(.15 + size * ease.out(a)); m.material.opacity = opacity * (1 - a) * Math.min(1, a * 8);
      }
    });
    return group;
  };

  /**
   * a packed football stadium in daylight. Pitch 68 x 105 m: x -34..34, z -52.5..52.5, centre spot 0,0,0, goals at
   * z = ±52.5. Two tiers of stands on all four sides (thousands of blocky fans in team colours that bob and jump on
   * K.cheer), a roof, LED boards around the pitch, banners on the tier fronts, the tunnel at x = -38 (mouth facing +x),
   * dugouts beside it. o: home (fan colour), alt (second colour), env, name (the ground's name on the stand roofs),
   * boards: ['TEXT', ...] (pitch-side adverts), banners: ['TEXT', ...] (between the tiers), mosaic: 'TEXT' (spelled out
   * by the fans in the side stands), fans: false (empty seats).
   * Returns { env, ball, goals: [near (z=+52.5), far (z=-52.5)], tunnel, crowdAt(x, z) -> a seat position, bench }.
   */
  K.stadiumSet = (o = {}) => {
    const env = K.env(o.env || 'day', { elev: 58, az: 30, exp: .64, fog: [0xd6e4f2, 300, 1600], hemi: [0xe4eeff, 0x6f8a52, 1.8] });
    const sun = env.sun.shadow.camera; sun.left = -130; sun.right = 130; sun.top = 130; sun.bottom = -130; sun.far = 600; sun.updateProjectionMatrix();
    env.sun.position.copy(env.sunDir.clone().multiplyScalar(250));
    const HOME = new THREE.Color(o.home ?? 0xd0021b), ALT = new THREE.Color(o.alt ?? 0xffffff);
    const hex = c => '#' + new THREE.Color(c).getHexString();
    K.ground('concrete', 2000);
    // the pitch: mowed stripes and every line in the right place
    const PW = 84, PL = 125, px = 24;
    const pitchTex = canvasTex(PW * px, PL * px, (c, w, h) => {
      const m2p = v => v * px, X = x => w / 2 + m2p(x), Z = z => h / 2 + m2p(z);
      c.fillStyle = '#3f9a35'; c.fillRect(0, 0, w, h);
      for (let k = -10; k < 10; k++) { c.fillStyle = k % 2 ? '#47a83b' : '#3d9433'; c.fillRect(X(-34), Z(k * 5.25), m2p(68), m2p(5.25)); }
      c.strokeStyle = 'rgba(255,255,255,.95)'; c.lineWidth = 4;
      const rect = (x0, z0, x1, z1) => c.strokeRect(X(x0), Z(z0), m2p(x1 - x0), m2p(z1 - z0));
      rect(-34, -52.5, 34, 52.5); c.beginPath(); c.moveTo(X(-34), Z(0)); c.lineTo(X(34), Z(0)); c.stroke();
      c.beginPath(); c.arc(X(0), Z(0), m2p(9.15), 0, 7); c.stroke();
      for (const s of [-1, 1]) {
        const g = 52.5 * s;
        rect(-20.16, Math.min(g, g - s * 16.5), 20.16, Math.max(g, g - s * 16.5));
        rect(-9.16, Math.min(g, g - s * 5.5), 9.16, Math.max(g, g - s * 5.5));
        c.fillStyle = '#fff'; c.beginPath(); c.arc(X(0), Z(g - s * 11), 5, 0, 7); c.fill();
        c.save(); c.beginPath(); c.rect(0, s > 0 ? 0 : Z(g - s * 16.5), w, s > 0 ? Z(g - s * 16.5) : h); c.clip();
        c.beginPath(); c.arc(X(0), Z(g - s * 11), m2p(9.15), 0, 7); c.stroke(); c.restore();
        for (const x of [-34, 34]) { c.beginPath(); c.arc(X(x), Z(g), m2p(1), 0, 7); c.stroke(); }
      }
      c.fillStyle = '#fff'; c.beginPath(); c.arc(X(0), Z(0), 5, 0, 7); c.fill();
    });
    pitchTex.wrapS = pitchTex.wrapT = THREE.ClampToEdgeWrapping; pitchTex.repeat.set(1, 1);
    const pitch = new THREE.Mesh(new THREE.PlaneGeometry(PW, PL), new THREE.MeshStandardMaterial({ map: pitchTex, roughness: .95 }));
    pitch.rotation.x = -Math.PI / 2; pitch.position.y = .01; pitch.receiveShadow = true; scene.add(pitch);
    const goals = [K.goal([0, 0, 52.5], -1), K.goal([0, 0, -52.5], 1)];
    for (const x of [-34, 34]) for (const z of [-52.5, 52.5]) {
      K.add(new THREE.Mesh(new THREE.CylinderGeometry(.02, .02, 1.5, 6), K.mat(0xffffff)), [x, .75, z]);
      K.add(new THREE.Mesh(new THREE.PlaneGeometry(.4, .3), K.mat(HOME, { side: THREE.DoubleSide })), [x + .2, 1.35, z]);
    }
    // LED boards around the pitch
    const boards = o.boards && o.boards.length ? o.boards : ['MATCHDAY', 'HOME OF FOOTBALL', 'SUPPORTERS CLUB'];
    const boardRun = (len, pos, rot) => {
      const n = Math.max(1, Math.round(len / 12));
      for (let k = 0; k < n; k++) {
        const along = -len / 2 + (k + .5) * len / n;
        const p = v3(pos).add(new THREE.Vector3(Math.cos(rot), 0, -Math.sin(rot)).multiplyScalar(along));
        K.sign(boards[k % boards.length], [p.x, .5, p.z], { w: len / n - .1, h: .95, rot, bg: k % 2 ? '#111216' : hex(HOME), fg: '#ffffff', size: .62, glow: true });
      }
    };
    boardRun(110, [-37.5, 0, 0], Math.PI / 2); boardRun(110, [37.5, 0, 0], -Math.PI / 2);
    boardRun(72, [0, 0, -56.5], 0); boardRun(72, [0, 0, 56.5], Math.PI);
    // stands: two tiers on each side; rows step .8 m back and .45 m (lower) / .6 m (upper) up
    const D0 = 41, E0 = 60, DEP = .8, LOW = 24, UP = 20, GAP = 3.2, SEAT = .62;
    const rowsY = [], rowsOff = [];
    for (let r = 0; r < LOW + UP; r++) {
      const up = r >= LOW, k = up ? r - LOW : r;
      rowsOff.push((up ? LOW * DEP + 1.4 : 0) + k * DEP);
      rowsY.push(up ? 1.2 + LOW * .45 + GAP + k * .62 : 1.2 + k * .45);
    }
    const concrete = K.mat(0x8f9399), seatsMat = K.mat(HOME.clone().multiplyScalar(.75));
    const sides = [ { axis: 'x', s: -1 }, { axis: 'x', s: 1 }, { axis: 'z', s: -1 }, { axis: 'z', s: 1 } ];
    // terraces as one stepped slab per tier per side
    for (const sd of sides) for (const [r0, r1] of [[0, LOW - 1], [LOW, LOW + UP - 1]]) {
      for (let r = r0; r <= r1; r += 2) {
        const off = rowsOff[r], y = rowsY[r];
        const halfLen = (sd.axis === 'x' ? E0 - 7.5 : D0 - 7) + off + 1;
        const g = new THREE.BoxGeometry(sd.axis === 'x' ? DEP * 2 : halfLen * 2, y, sd.axis === 'x' ? halfLen * 2 : DEP * 2);
        const m = new THREE.Mesh(g, concrete);
        const base = (sd.axis === 'x' ? D0 : E0) + off + DEP * .5;
        m.position.set(sd.axis === 'x' ? sd.s * base : 0, y / 2 - .25, sd.axis === 'x' ? 0 : sd.s * base);
        m.receiveShadow = true; scene.add(m);
      }
    }
    // tier-front banners, back walls and the roof
    const banners = o.banners && o.banners.length ? o.banners : [o.name || 'THE HOME END', 'SINCE 1878'];
    let bi = 0;
    for (const sd of sides) {
      const off = rowsOff[LOW] - .6, len = (sd.axis === 'x' ? E0 - 7.5 : D0 - 7) * 2 + off * 2;
      const base = (sd.axis === 'x' ? D0 : E0) + off, n = Math.max(1, Math.round(len / 26));
      for (let k = 0; k < n; k++) {
        const along = -len / 2 + (k + .5) * len / n, rot = sd.axis === 'x' ? -sd.s * Math.PI / 2 : (sd.s > 0 ? Math.PI : 0);
        const p = sd.axis === 'x' ? [sd.s * base, rowsY[LOW - 1] + 1.6, along] : [along, rowsY[LOW - 1] + 1.6, sd.s * base];
        K.sign(banners[bi++ % banners.length], p, { w: len / n - .2, h: 2.2, rot, bg: (k + bi) % 2 ? '#15171c' : hex(HOME), fg: '#ffffff', size: .55, glow: true });
      }
      const top = rowsY[LOW + UP - 1] + 4, back = (sd.axis === 'x' ? D0 : E0) + rowsOff[LOW + UP - 1] + 2;
      const half = (sd.axis === 'x' ? E0 - 7.5 : D0 - 7) + rowsOff[LOW + UP - 1] + 3;
      const wall = new THREE.Mesh(new THREE.BoxGeometry(sd.axis === 'x' ? 1 : half * 2, top + 2, sd.axis === 'x' ? half * 2 : 1), K.mat(0x7c8088));
      wall.position.set(sd.axis === 'x' ? sd.s * back : 0, (top + 2) / 2 - .5, sd.axis === 'x' ? 0 : sd.s * back); scene.add(wall);
      const depth = rowsOff[LOW + UP - 1] + 14;
      const roof = new THREE.Mesh(new THREE.BoxGeometry(sd.axis === 'x' ? depth : half * 2, .5, sd.axis === 'x' ? half * 2 : depth), K.mat(0xb8bec6, { roughness: .6, emissive: 0x6a6f76 }));
      roof.position.set(sd.axis === 'x' ? sd.s * (back - depth / 2 + 1) : 0, top + 2.4, sd.axis === 'x' ? 0 : sd.s * (back - depth / 2 + 1));
      roof.rotation[sd.axis === 'x' ? 'z' : 'x'] = (sd.axis === 'x' ? -sd.s : sd.s) * .07; roof.castShadow = true; scene.add(roof);
      const lip = new THREE.Mesh(new THREE.BoxGeometry(sd.axis === 'x' ? 1.2 : half * 2, 1.4, sd.axis === 'x' ? half * 2 : 1.2), K.mat(0xf2f4f6, { roughness: .4 }));
      lip.position.set(sd.axis === 'x' ? sd.s * (back - depth + 1.4) : 0, top + 1.6, sd.axis === 'x' ? 0 : sd.s * (back - depth + 1.4)); scene.add(lip);
      for (let k = -half + 6; k < half; k += 12) {
        const truss = new THREE.Mesh(new THREE.BoxGeometry(sd.axis === 'x' ? depth : .3, .3, sd.axis === 'x' ? .3 : depth), K.mat(0xffffff));
        truss.position.copy(roof.position); truss.position.y -= .4; truss.position[sd.axis === 'x' ? 'z' : 'x'] = k; truss.rotation.copy(roof.rotation); scene.add(truss);
      }
      if (o.name && sd.axis === 'x') K.sign(o.name.toUpperCase(), [sd.s * (back - depth + 1.4 - sd.s * .62), top + 1.6, 0], { w: 46, h: 1.3, rot: -sd.s * Math.PI / 2, bg: '#f2f4f6', fg: hex(HOME), size: .8 });
    }
    // the fans: instanced blocky people (body + head) in every seat, bobbing; jumping when K.cheer is on
    const seats = [];
    const mosaic = o.mosaic ? (() => {
      const cv = document.createElement('canvas'); cv.width = 1024; cv.height = 96; const c = cv.getContext('2d', { willReadFrequently: true });
      c.fillStyle = '#000'; c.fillRect(0, 0, 1024, 96); c.fillStyle = '#fff'; c.textAlign = 'center'; c.textBaseline = 'middle';
      let size = 80; c.font = `900 ${size}px ${SANS}`; const mw = c.measureText(o.mosaic).width; if (mw > 980) { size *= 980 / mw; c.font = `900 ${size}px ${SANS}`; }
      c.fillText(String(o.mosaic).toUpperCase(), 512, 50);
      const d = c.getImageData(0, 0, 1024, 96).data; return (u, v) => d[(Math.floor(clamp01(v) * 95) * 1024 + Math.floor(clamp01(u) * 1023)) * 4] > 128;
    })() : null;
    for (const sd of sides) for (let r = 0; r < LOW + UP; r++) {
      const off = rowsOff[r], y = rowsY[r] - .25;
      const half = (sd.axis === 'x' ? E0 - 7.5 : D0 - 7) + off, base = (sd.axis === 'x' ? D0 : E0) + off + DEP * .55;
      const n = Math.floor(half * 2 / SEAT);
      for (let k = 0; k < n; k++) {
        const along = -half + (k + .5) * SEAT + (Math.random() - .5) * .08;
        const x = sd.axis === 'x' ? sd.s * base : along, z = sd.axis === 'x' ? along : sd.s * base;
        let col;
        if (mosaic && sd.axis === 'x' && r < LOW) {
          const u = sd.s < 0 ? (half - along) / (half * 2) : (along + half) / (half * 2);
          col = mosaic(u, 1 - r / (LOW - 1)) ? ALT : HOME;
        } else { const q = Math.random(); col = q < .78 ? HOME : q < .93 ? ALT : new THREE.Color(0x24262c); }
        seats.push({ x, y, z, rot: sd.axis === 'x' ? -sd.s * Math.PI / 2 : (sd.s > 0 ? Math.PI : 0), col, empty: o.fans === false || Math.random() < .015 });
      }
    }
    const U = { uTime: { value: 0 }, uCheer: { value: 0 } };
    const crowdMat = () => { const m = new THREE.MeshLambertMaterial({ color: 0xffffff });
      m.onBeforeCompile = sh => { Object.assign(sh.uniforms, U);
        sh.vertexShader = 'uniform float uTime; uniform float uCheer;\n' + sh.vertexShader.replace('#include <begin_vertex>', `#include <begin_vertex>
          vec4 ip = instanceMatrix[3];
          float ph = fract(sin(dot(ip.xz, vec2(12.9898, 78.233))) * 43758.5453);
          transformed.y += uCheer * max(0.0, sin(uTime * (8.0 + ph * 5.0) + ph * 6.283)) * 0.28 + sin(uTime * 1.4 + ph * 6.283) * 0.015;`); };
      return m; };
    const live = seats.filter(s => !s.empty);
    const bodyG = new THREE.BoxGeometry(.44, .62, .3); bodyG.translate(0, .31, 0);
    const headG = new THREE.BoxGeometry(.22, .24, .22); headG.translate(0, .76, 0);
    const armG = new THREE.BoxGeometry(.56, .1, .1); armG.translate(0, .62, 0);
    const bodies = new THREE.InstancedMesh(bodyG, crowdMat(), live.length), heads = new THREE.InstancedMesh(headG, crowdMat(), live.length);
    const SKINS = [0xf1c9a5, 0xe0ac7e, 0xc68642, 0x8d5524, 0x5c3a1e, 0xf5d0b0];
    const mtx = new THREE.Matrix4(), q = new THREE.Quaternion(), sc = new THREE.Vector3(1, 1, 1), up = new THREE.Vector3(0, 1, 0);
    live.forEach((s, i) => {
      q.setFromAxisAngle(up, s.rot); sc.set(1, .92 + Math.random() * .16, 1);
      mtx.compose(new THREE.Vector3(s.x, s.y, s.z), q, sc); bodies.setMatrixAt(i, mtx); heads.setMatrixAt(i, mtx);
      bodies.setColorAt(i, s.col.clone().offsetHSL(0, 0, (Math.random() - .5) * .08)); heads.setColorAt(i, new THREE.Color(SKINS[i % SKINS.length]));
    });
    for (const m of [bodies, heads]) { m.instanceMatrix.needsUpdate = true; m.instanceColor.needsUpdate = true; m.frustumCulled = false; scene.add(m); }
    updaters.push(t => { U.uTime.value = t; U.uCheer.value = K.cheerLevel ? K.cheerLevel(t) : 0; });
    // tunnel and dugouts on the -x side, halfway
    const tunnel = K.tunnel([-38.2, 0, 0], Math.PI / 2, { color: hex(HOME) });
    const bench = [];
    for (const z of [-9, 9]) {
      K.box([-38.5, 1.1, z], [2.4, .12, 7], 0xdfe3e8, { transparent: true, opacity: .55 });
      K.box([-39.6, .6, z], [.2, 1.2, 7], 0x2b2f36);
      for (let k = -3; k <= 3; k++) { const s = K.box([-39.2, .45, z + k * .9], [.5, .1, .5], 0x1d3a8a); bench.push(s.position.clone()); }
    }
    const ball = K.soccerBall([0, .11, 0]);
    const crowdAt = (x, z) => { let best = null, d = 1e9; for (const s of seats) { const e = (s.x - x) ** 2 + (s.z - z) ** 2; if (e < d) { d = e; best = s; } } return best ? new THREE.Vector3(best.x, best.y, best.z) : null; };
    return { env, ball, goals, tunnel, bench, crowdAt, dims: { halfW: 34, halfL: 52.5 }, fans: { cheer: K.cheer } };
  };
  K.sets.stadium = (o = {}) => K.stadiumSet(o);

  // ── camera ─────────────────────────────────────────────────────────────────
  /** keyframes [{t, pos:[x,y,z], look:[x,y,z], fov?, ease?}]: the camera glides between them every frame */
  K.camPath = (keys) => { camKeys = keys.map(k => Object.assign({}, k, { pos: v3(k.pos), look: v3(k.look) })).sort((a, b) => a.t - b.t); };
  K.lookAt = (pos, look, fov) => { camera.position.copy(v3(pos)); camera.lookAt(v3(look)); if (fov) { camera.fov = fov; camera.updateProjectionMatrix(); } };
  K.shake = (t0, dur = 1, amp = .3) => shakes.push({ t0, dur, amp });
  K.handheld = (amount = .6) => { handheld = amount; };
  /** hard-cut shots, TV style: [{t, pos, look, fov?, to?: {pos?, look?, fov?}, ease?, follow?: object, lookOffset?, chase?}]
   *  Each shot holds from its t until the next one starts (a clean cut), drifting from pos/look toward `to`.
   *  follow: an object the camera keeps looking at (+lookOffset); chase: [dx,dy,dz] keeps the camera at follow + chase. */
  let shots = null;
  K.shots = (list) => { shots = list.map(s => Object.assign({}, s, { pos: s.pos ? v3(s.pos) : null, look: s.look ? v3(s.look) : null,
    to: s.to ? { pos: s.to.pos ? v3(s.to.pos) : null, look: s.to.look ? v3(s.to.look) : null, fov: s.to.fov } : null })).sort((a, b) => a.t - b.t); };
  const _sp = new THREE.Vector3(), _sl = new THREE.Vector3(), _sf = new THREE.Vector3();
  function runShots(t) {
    if (!shots || !shots.length) return;
    let i = 0; while (i < shots.length - 1 && shots[i + 1].t <= t) i++;
    const s = shots[i], end = i < shots.length - 1 ? shots[i + 1].t : data.duration;
    const x = (ease[s.ease || 'inOut'] || ease.inOut)((t - s.t) / Math.max(.01, end - s.t));
    const f = s.follow ? s.follow.getWorldPosition(_sf) : null;
    if (s.chase && f) _sp.copy(f).add(v3(s.chase)); else _sp.copy(s.pos || camera.position); if (!s.chase && s.to && s.to.pos) _sp.lerp(s.to.pos, x);
    if (f) _sl.copy(f).add(v3(s.lookOffset || [0, 1, 0])); else { _sl.copy(s.look || [0, 0, 0]); if (s.to && s.to.look) _sl.lerp(s.to.look, x); }
    camera.position.copy(_sp); camera.lookAt(_sl);
    const fa = s.fov || 62, fb = (s.to && s.to.fov) || fa; camera.fov = fa + (fb - fa) * x; camera.updateProjectionMatrix();
  }
  function runCamPath(t) {
    if (!camKeys || !camKeys.length) return;
    let i = 0; while (i < camKeys.length - 1 && camKeys[i + 1].t <= t) i++;
    const a = camKeys[i], b = camKeys[Math.min(i + 1, camKeys.length - 1)];
    const x = a === b ? 0 : (ease[b.ease || 'inOut'] || ease.inOut)((t - a.t) / (b.t - a.t));
    camera.position.lerpVectors(a.pos, b.pos, x);
    camera.lookAt(new THREE.Vector3().lerpVectors(a.look, b.look, x));
    const fa = a.fov || 62, fb = b.fov || fa; camera.fov = fa + (fb - fa) * x; camera.updateProjectionMatrix();
  }

  // ── on-screen text: hook title, HUD counter, captions, flash, end card ─────
  // Drawn on a 2D canvas over the finished 3D frame, so every grabbed frame is exactly what we drew.
  const out = document.createElement('canvas'); out.width = W; out.height = H;
  const ctx = out.getContext('2d');
  const SERIF = 'Charter, "Iowan Old Style", Georgia, serif', SANS = '-apple-system, "Helvetica Neue", Helvetica, sans-serif';
  const BLUE = '#a9c8ff';
  /** words of `text`, each flagged when it is inside one of the emphasised phrases */
  function words(text, phrases = []) {
    text = String(text || ''); const mark = new Array(text.length).fill(false);
    for (const ph of phrases || []) { if (!ph) continue; let i = text.toLowerCase().indexOf(String(ph).toLowerCase());
      while (i >= 0) { for (let k = i; k < i + ph.length; k++) mark[k] = true; i = text.toLowerCase().indexOf(String(ph).toLowerCase(), i + 1); } }
    const out = []; let pos = 0;
    for (const w of text.split(/\s+/)) { if (!w) continue; const i = text.indexOf(w, pos); pos = i + w.length;
      out.push({ w, em: mark.slice(i, i + w.length).some(Boolean) }); }
    return out;
  }
  /** centred, wrapped text with highlighted words; returns the height used */
  // how bright the frame is behind a strip of text (0..1), from a tiny copy of that strip
  const probe = document.createElement('canvas'); probe.width = 32; probe.height = 8;
  const pctx = probe.getContext('2d', { willReadFrequently: true });
  function brightness(y, h) {
    pctx.drawImage(renderer.domElement, 60, y, W - 170, h, 0, 0, 32, 8);
    const d = pctx.getImageData(0, 0, 32, 8).data; let sum = 0;
    for (let i = 0; i < d.length; i += 4) sum += .2126 * d[i] + .7152 * d[i + 1] + .0722 * d[i + 2];
    return sum / (d.length / 4) / 255;
  }
  function block(text, phrases, cx, y, maxW, font, size, lh, alpha, rise = 0) {
    if (alpha <= 0.001 || !text) return 0;
    // white text on a bright frame (a flash, plasma, clouds) gets a soft dark band behind it
    const est = size * lh * Math.max(1, Math.ceil(String(text).length * size * .5 / maxW));
    const lum = brightness(y, est);
    if (lum > .45) {
      ctx.save(); ctx.globalAlpha = alpha * Math.min(.62, (lum - .45) * 2.2);
      const g = ctx.createLinearGradient(0, y - 60, 0, y + est + 60);
      g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(.25, 'rgba(0,0,0,1)'); g.addColorStop(.75, 'rgba(0,0,0,1)'); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g; ctx.fillRect(0, y - 60, W, est + 120); ctx.restore();
    }
    ctx.font = `${font.replace('SIZE', size + 'px')}`;
    const ws = words(text, phrases), space = ctx.measureText(' ').width, lines = [[]]; let lw = 0;
    for (const x of ws) { x.width = ctx.measureText(x.w).width;
      if (lw > 0 && lw + space + x.width > maxW) { lines.push([]); lw = 0; }
      lines[lines.length - 1].push(x); lw += (lw > 0 ? space : 0) + x.width; }
    ctx.save(); ctx.globalAlpha = alpha; ctx.textBaseline = 'top';
    lines.forEach((ln, li) => {
      const tot = ln.reduce((s, x) => s + x.width, 0) + space * (ln.length - 1); let x = cx - tot / 2;
      for (const wd of ln) {
        for (const [blur, col] of [[22, 'rgba(0,0,0,.75)'], [4, 'rgba(0,0,0,.7)']]) {
          ctx.shadowColor = col; ctx.shadowBlur = blur; ctx.fillStyle = wd.em ? BLUE : '#fff';
          ctx.fillText(wd.w, x, y + li * size * lh + rise);
        }
        x += wd.width + space;
      }
    });
    ctx.restore();
    return lines.length * size * lh;
  }
  function label(text, x, y, size, alpha, bold = 700, spacing = .15) {
    if (!text || alpha <= 0) return;
    ctx.save(); ctx.globalAlpha = alpha; ctx.font = `${bold} ${size}px ${SANS}`; ctx.textBaseline = 'top';
    ctx.shadowColor = 'rgba(0,0,0,.7)'; ctx.shadowBlur = 12; ctx.fillStyle = '#fff';
    let cx = x; for (const ch of String(text).toUpperCase()) { ctx.fillText(ch, cx, y); cx += ctx.measureText(ch).width + size * spacing; }
    ctx.restore();
  }
  const flashes = [], fades = [];
  let vignette = true;
  K.flash = (t0, dur = .6, color = '#ffffff') => flashes.push({ t0, dur, color });
  K.fadeOut = (t0, dur = .6) => fades.push({ t0, dur, dir: 1 });
  K.fadeIn = (t0, dur = .6) => fades.push({ t0, dur, dir: -1 });
  K.vignette = (on = true) => { vignette = on; };
  K.hideText = false;
  const fmt = (x, h) => {
    const d = h.decimals || 0;
    if (h.kind === 'timer') { const m = Math.floor(x / 60), s = x - m * 60; return `${m}:${s.toFixed(2).padStart(5, '0')}`; }
    return x.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const vigGrad = (() => { const g = ctx.createRadialGradient(W / 2, H * .45, H * .3, W / 2, H * .45, H * .62);
    g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,.5)'); return g; })();
  // ── the narrated style: word-by-word yellow captions, broadcast cards, a scorebug ──
  const NARR = data.style === 'narrated';
  if (NARR) { vignette = false; handheld = .2; bloom.strength = .12; }
  function rr(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }
  function caption(text, y) {
    ctx.save(); ctx.font = `800 50px ${SANS}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    const lines = []; let cur = '';
    for (const w of String(text).toLowerCase().split(/\s+/)) { const tryL = cur ? cur + ' ' + w : w; if (cur && ctx.measureText(tryL).width > 820) { lines.push(cur); cur = w; } else cur = tryL; }
    if (cur) lines.push(cur);
    lines.forEach((l, i) => { const yy = y + i * 60;
      ctx.shadowColor = 'rgba(0,0,0,.55)'; ctx.shadowBlur = 10; ctx.lineWidth = 9; ctx.strokeStyle = '#111'; ctx.strokeText(l, 520, yy);
      ctx.shadowBlur = 0; ctx.fillStyle = '#ffe11a'; ctx.fillText(l, 520, yy); });
    ctx.restore();
  }
  const CHIP = { W: ['#2fb84f', '#fff'], L: ['#d62d2d', '#fff'], D: ['#8a8f98', '#fff'], '?': ['#ffd21a', '#1a1a1a'] };
  function card(c, t, t0, t1) {
    const a = Math.min(clamp01((t - t0) / .18), clamp01((t1 - t) / .18));
    if (a <= 0) return;
    const accent = c.color || '#d0021b', x = 70, w = 860; let y = 300 - (1 - ease.out((t - t0) / .3)) * 30;
    ctx.save(); ctx.globalAlpha = a; ctx.textBaseline = 'middle';
    if (c.kind === 'pill') {
      ctx.font = `900 46px ${SANS}`; const tw = ctx.measureText(String(c.text || '').toUpperCase()).width + 70;
      ctx.fillStyle = '#16205a'; rr(520 - tw / 2, y, tw, 78, 14); ctx.fill(); ctx.lineWidth = 4; ctx.strokeStyle = '#ffffff'; ctx.stroke();
      ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.fillText(String(c.text || '').toUpperCase(), 520, y + 41); ctx.restore(); return;
    }
    if (c.kind === 'count' || c.kind === 'timer' || c.kind === 'text') {
      let v;
      if (c.kind === 'text') v = String(c.text || '');
      else { const xx = ease[c.kind === 'timer' ? 'linear' : 'out']((t - t0) / Math.max(.3, c.kind === 'timer' ? (t1 - t0) : Math.min(1.2, (t1 - t0) * .6)));
        v = fmt(Number(c.from || 0) + (Number(c.to || 0) - Number(c.from || 0)) * xx, c) + (c.unit || ''); }
      ctx.textAlign = 'center'; ctx.lineJoin = 'round';
      if (c.label) { ctx.font = `900 34px ${SANS}`; ctx.lineWidth = 7; ctx.strokeStyle = '#111'; ctx.strokeText(c.label.toUpperCase(), 520, y + 10); ctx.fillStyle = '#fff'; ctx.fillText(c.label.toUpperCase(), 520, y + 10); y += 40; }
      const size = Math.min(110, Math.floor(900 / (Math.max(1, v.length) * .6)));
      ctx.font = `900 ${size}px ${SANS}`; ctx.lineWidth = 12; ctx.strokeStyle = '#111'; ctx.strokeText(v, 520, y + size * .6);
      ctx.fillStyle = '#ffd21a'; ctx.fillText(v, 520, y + size * .6);
      if (c.sub) { ctx.font = `800 30px ${SANS}`; ctx.lineWidth = 6; ctx.strokeText(c.sub.toUpperCase(), 520, y + size * 1.35); ctx.fillStyle = '#fff'; ctx.fillText(c.sub.toUpperCase(), 520, y + size * 1.35); }
      ctx.restore(); return;
    }
    // panel: [tag] / header bar / rows (text, bar, score, hi) / chips
    if (c.tag) {
      ctx.font = `italic 900 40px ${SANS}`; const tw = ctx.measureText(c.tag.toUpperCase()).width;
      ctx.fillStyle = '#0d1236'; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + tw + 46, y); ctx.lineTo(x + tw + 30, y + 54); ctx.lineTo(x, y + 54); ctx.fill();
      ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.fillText(c.tag.toUpperCase(), x + 14, y + 29); y += 54;
    }
    ctx.fillStyle = '#141b45'; rr(x, y, w, 80, [10, 10, 0, 0]); ctx.fill();
    ctx.fillStyle = accent; ctx.fillRect(x, y + 74, w, 6);
    ctx.fillStyle = '#fff'; ctx.textAlign = 'left'; ctx.font = `900 46px ${SANS}`; ctx.fillText(String(c.title || '').toUpperCase(), x + 26, y + 40); y += 80;
    const rows = c.rows || [], chips = c.chips || [];
    let h = 26 + rows.reduce((s, r) => s + (r.bar != null ? 104 : 84), 0) + (chips.length ? 92 : 0);
    ctx.shadowColor = 'rgba(0,0,0,.3)'; ctx.shadowBlur = 18; ctx.fillStyle = '#f7f8fa'; rr(x, y, w, h, [0, 0, 12, 12]); ctx.fill(); ctx.shadowBlur = 0;
    let yy = y + 16;
    rows.forEach((r, i) => {
      const ra = clamp01((t - t0 - .25 - i * .45) / .2), rh = r.bar != null ? 104 : 84;
      if (ra > 0) {
        ctx.globalAlpha = a * ra;
        const hiOn = r.hi && t - t0 - .25 - i * .45 > .35;
        if (hiOn) { ctx.fillStyle = '#ffe11a'; rr(x + 14, yy, w - 28, rh - 12, 8); ctx.fill(); }
        ctx.fillStyle = '#121418'; ctx.font = `800 44px ${SANS}`; ctx.textAlign = 'left'; ctx.fillText(String(r.text || '').toUpperCase(), x + 30, yy + 38);
        if (r.score != null) { ctx.fillStyle = '#141b45'; rr(x + w - 96, yy + 6, 66, 64, 8); ctx.fill(); ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.font = `900 46px ${SANS}`; ctx.fillText(String(r.score), x + w - 63, yy + 39); }
        if (r.bar != null) { ctx.fillStyle = '#dfe2e8'; rr(x + 30, yy + 74, w - 60, 14, 7); ctx.fill();
          ctx.fillStyle = r.barColor || accent; rr(x + 30, yy + 74, Math.max(14, (w - 60) * clamp01(Number(r.bar)) * ease.out(ra * clamp01((t - t0 - .25 - i * .45) / .7))), 14, 7); ctx.fill(); }
      }
      yy += rh;
    });
    chips.forEach((ch, k) => {
      const ca = clamp01((t - t0 - .3 - rows.length * .45 - k * .16) / .12); if (ca <= 0) return;
      const [bg, fg] = CHIP[String(ch).toUpperCase()] || CHIP.D, s = 64 * (ca < 1 ? .7 + .3 * ease.back(ca) : 1), cx = x + 30 + k * 80 + 32, cy = yy + 38;
      ctx.globalAlpha = a * ca; ctx.fillStyle = bg; rr(cx - s / 2, cy - s / 2, s, s, 8); ctx.fill();
      ctx.fillStyle = fg; ctx.textAlign = 'center'; ctx.font = `900 ${Math.round(s * .66)}px ${SANS}`; ctx.fillText(String(ch).toUpperCase(), cx, cy + 2);
    });
    ctx.restore();
  }
  function scorebug(sc, a) {
    if (!sc || a <= 0) return;
    ctx.save(); ctx.globalAlpha = a; ctx.textBaseline = 'middle';
    const x = 60, y = 190, txt = `${String(sc.home || '').toUpperCase()} ${sc.h ?? 0} - ${sc.a ?? 0} ${String(sc.away || '').toUpperCase()}`;
    ctx.font = `900 40px ${SANS}`; const w = ctx.measureText(txt).width + 44;
    if (sc.clock) { ctx.fillStyle = '#d0021b'; rr(x, y, Math.max(150, w * .45), 40, [8, 8, 0, 0]); ctx.fill(); ctx.fillStyle = '#fff'; ctx.font = `800 28px ${SANS}`; ctx.textAlign = 'center'; ctx.fillText(sc.clock, x + Math.max(150, w * .45) / 2, y + 21); }
    ctx.fillStyle = '#0c0d12'; rr(x, y + 40, w, 64, [0, 8, 8, 8]); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = `900 40px ${SANS}`; ctx.textAlign = 'left'; ctx.fillText(txt, x + 22, y + 73);
    ctx.restore();
  }
  function narratedText(t) {
    if (K.hideText) return;
    // the scorebug: set by a beat's "score", kept until another beat changes or hides it
    let sc = null, scFrom = 0;
    for (const b of beats) if (b.start <= t && b.score !== undefined && b.score !== null) { sc = b.score === 'hide' ? null : b.score; scFrom = b.start; }
    let cardOn = false;
    for (let i = 0; i < beats.length; i++) {
      const b = beats[i], c = b.card || b.hud; if (!c) continue;
      const t1 = beats[Math.min(beats.length - 1, i + Math.max(1, c.hold || 1) - 1)].end;
      if (t >= b.start && t < t1) { card(c, t, b.start, t1); cardOn = cardOn || c.kind !== 'count'; }
    }
    if (sc && !cardOn) scorebug(sc, clamp01((t - scFrom) / .25));
    for (const b of beats) for (const ch of b.chunks || []) if (t >= ch.t0 && t < ch.t1) caption(ch.text, 1240);
  }
  function overlay(t) {
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(renderer.domElement, 0, 0);
    if (vignette) { ctx.fillStyle = vigGrad; ctx.fillRect(0, 0, W, H); }
    const endStart = data.end_card ? data.duration - data.end_card : 1e9;
    const show = K.hideText ? 0 : 1;
    if (NARR) { narratedText(t); flashFade(t); return; }
    // the hook question sits on screen through beat 0 (TikTok's buttons cover the right edge and bottom ~380px)
    const b0 = beats[0] || { start: 0, end: 3 };
    block(data.title, data.highlight, 520, 470, 840, `500 SIZE ${SERIF}`, 70, 1.18, show * Math.min(clamp01(t / .35), 1 - clamp01((t - b0.end + .1) / .4)));
    let bi = -1; for (let i = 0; i < beats.length; i++) if (t >= beats[i].start && t < beats[i].end) bi = i;
    const b = beats[bi];
    if (b && bi > 0 && t < endStart) {
      const a = Math.min(clamp01((t - b.start) / .22), clamp01((b.end - t) / .22));
      block(b.caption || b.say, b.emph, 520, 1130, 860, `italic 400 SIZE ${SERIF}`, 62, 1.2, show * a, (1 - ease.out((t - b.start) / .3)) * 14);
    }
    let h = null, hs = 0, he = 0;
    for (let i = 0; i < beats.length; i++) if (beats[i].hud && beats[i].start <= t) { h = beats[i].hud; hs = beats[i].start; he = beats[i].end; }
    if (h && t < endStart) {
      let v;
      if (h.kind === 'count' || h.kind === 'timer') {
        const x = ease[h.kind === 'timer' ? 'linear' : 'out']((t - hs) / Math.max(.3, h.kind === 'timer' ? (he - hs) : Math.min(1.2, (he - hs) * .6)));
        const from = Number(h.from || 0), to = Number(h.to || 0);
        v = fmt(from + (to - from) * x, h) + (h.unit || '');
      } else v = String(h.text || '');
      const a = show * clamp01((t - hs) / .25);
      label(h.label, 64, 170, 23, a);
      const size = Math.min(92, Math.floor(900 / (Math.max(1, v.length) * .53)));
      ctx.save(); ctx.globalAlpha = a; ctx.font = `400 ${size}px ${SERIF}`; ctx.textBaseline = 'top';
      ctx.shadowColor = 'rgba(0,0,0,.7)'; ctx.shadowBlur = 16; ctx.fillStyle = '#fff'; ctx.fillText(v, 62, 202); ctx.restore();
      label(h.sub, 64, 210 + size * 1.04, 21, a * .8, 600, .13);
    }
    flashFade(t);
    // the end card repeats the hook on dark, so the video loops back into its own first second
    const ea = clamp01((t - endStart) / .5);
    if (ea > 0) {
      ctx.save(); ctx.globalAlpha = ea; ctx.fillStyle = '#111'; ctx.fillRect(0, 0, W, H); ctx.restore();
      const used = block(data.title, data.highlight, 520, 820, 860, `500 SIZE ${SERIF}`, 70, 1.2, ea);
      if (data.end_fact) block(data.end_fact.toUpperCase(), [], 520, 850 + used, 780, `600 SIZE ${SANS}`, 22, 1.6, ea * .55);
    }
  }
  function flashFade(t) {
    let fo = 0, fc = '#fff';
    for (const f of flashes) { const a = (t - f.t0) / f.dur; if (a >= 0 && a < 1) { const v = a < .12 ? a / .12 : 1 - ease.out((a - .12) / .88); if (v > fo) { fo = v; fc = f.color; } } }
    if (fo > 0) { ctx.save(); ctx.globalAlpha = fo; ctx.fillStyle = fc; ctx.fillRect(0, 0, W, H); ctx.restore(); }
    let fd = 0;
    for (const f of fades) if (t >= f.t0) fd = f.dir > 0 ? clamp01((t - f.t0) / f.dur) : 1 - clamp01((t - f.t0) / f.dur);
    if (fd > 0) { ctx.save(); ctx.globalAlpha = fd; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); ctx.restore(); }
  }
  window.__grab = (q = .9) => out.toDataURL('image/jpeg', q);

  // ── load the video's own scene, then hand frames to the recorder ───────────
  const mod = await import(sceneUrl);
  const sceneApi = (await mod.default(K)) || {};
  const dt = 1 / (data.fps || 30);
  const shakeOff = new THREE.Vector3();
  window.__frame = (t) => {
    runCamPath(t);
    for (const u of updaters) u(t, dt);
    if (sceneApi.update) sceneApi.update(t, dt);
    runShots(t);
    // camera shake + a gentle handheld sway, added after the scene placed the camera, removed after drawing
    let amp = 0;
    for (const s of shakes) { const a = (t - s.t0) / s.dur; if (a >= 0 && a < 1) amp += s.amp * (1 - a) * (1 - a); }
    shakeOff.set(noise.noise(t * 9, 1.3, 0) * amp + noise.noise(t * .6, 5.1, 0) * .05 * handheld,
                 noise.noise(t * 9, 7.7, 0) * amp + noise.noise(t * .5, 9.2, 0) * .04 * handheld,
                 noise.noise(t * 9, 3.1, 0) * amp * .5);
    camera.position.add(shakeOff);
    camera.updateMatrixWorld();
    overlay(t);
    composer.render();
    camera.position.sub(shakeOff);
  };
  return true;
}
