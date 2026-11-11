// Real-looking people for the viral videos: Quaternius "Universal Base Characters" (CC0, skinned, textured)
// animated with the Quaternius "Universal Animation Library" (CC0, same skeleton). Clothes are painted on in the
// shader from the skin weights (shirt = spine bones, shorts = pelvis/thighs, ...), so one body mesh can be a
// basketball player, a boxer, a guy in a hoodie or a red-skinned devil.
// Animation is stateless: p.play(clip, time) writes the pose for that exact time, so frames render in any order.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import * as SkeletonUtils from 'three/addons/utils/SkeletonUtils.js';

const BASE = './humans/';
const HAIRS = { buzzed: 'Hair_Buzzed', parted: 'Hair_SimpleParted', long: 'Hair_Long', buns: 'Hair_Buns', buzzed_f: 'Hair_BuzzedFemale' };
// clips by friendly name (the library's own names work too)
export const CLIPS = {
  idle: 'Idle_Loop', talk: 'Idle_Talking_Loop', walk: 'Walk_Loop', strut: 'Walk_Formal_Loop', jog: 'Jog_Fwd_Loop', sprint: 'Sprint_Loop',
  jump_start: 'Jump_Start', jump_air: 'Jump_Loop', jump_land: 'Jump_Land', punch: 'Punch_Cross', jab: 'Punch_Jab',
  hit_chest: 'Hit_Chest', hit_head: 'Hit_Head', death: 'Death01', roll: 'Roll', dance: 'Dance_Loop', sit: 'Sitting_Idle_Loop',
  sit_talk: 'Sitting_Talking_Loop', sit_down: 'Sitting_Enter', stand_up: 'Sitting_Exit', crouch: 'Crouch_Idle_Loop', sneak: 'Crouch_Fwd_Loop',
  cast: 'Spell_Simple_Shoot', cast_idle: 'Spell_Simple_Idle_Loop', swing: 'Sword_Attack', guard: 'Sword_Idle', push: 'Push_Loop',
  swim: 'Swim_Fwd_Loop', tread: 'Swim_Idle_Loop', drive: 'Driving_Loop', pickup: 'PickUp_Table', interact: 'Interact', kneel: 'Fixing_Kneeling',
  torch: 'Idle_Torch_Loop', tpose: 'A_TPose',
};
const SKIN = { light: ['light', 0xffffff], pale: ['light', 0xfff2ea], tan: ['light', 0xd9b08c], brown: ['dark', 0xffffff], dark: ['dark', 0x8a6a58],
               red: ['light', 0xff3a26], devil: ['light', 0xd8261a], green: ['light', 0x7fbf6a], blue: ['light', 0x6a8cff], grey: ['light', 0x9a9a9a] };

let LIB = null;          // { m: {gltf, textures...}, f: {...}, hair: {name: geometry/material}, clips: {name: clip} }

export async function loadHumans() {
  if (LIB) return LIB;
  const loader = new GLTFLoader().setPath(BASE);
  const tl = new THREE.TextureLoader().setPath(BASE);
  const tex = (f) => { const t = tl.load(f); t.colorSpace = THREE.SRGBColorSpace; t.flipY = false; return t; };
  const [m, f, anims, ...hairs] = await Promise.all([
    loader.loadAsync('Superhero_Male_FullBody.gltf'), loader.loadAsync('Superhero_Female_FullBody.gltf'), loader.loadAsync('anims.glb'),
    ...Object.values(HAIRS).concat(['Hair_Beard', 'Eyebrows_Female']).map(n => loader.loadAsync(n + '.gltf')),
  ]);
  const hair = {};
  Object.values(HAIRS).concat(['Hair_Beard', 'Eyebrows_Female']).forEach((n, i) => {
    let mesh = null; hairs[i].scene.traverse(o => { if (o.isSkinnedMesh) mesh = o; });
    hair[n] = mesh;
  });
  const clips = {};
  const restPelvis = (g) => { let p = null; g.scene.traverse(o => { if (o.name === 'pelvis') p = o.position.clone(); }); return p; };
  const animPelvis = restPelvis(anims);
  for (const c of anims.animations) clips[c.name] = c;
  LIB = {
    m: { gltf: m, skin: { light: tex('T_Superhero_Male_Ligh.png'), dark: tex('T_Superhero_Male_Dark.png') }, pelvis: restPelvis(m) },
    f: { gltf: f, skin: { light: tex('T_Superhero_Female_Light_BaseColor.png'), dark: tex('T_Superhero_Female_Dark_BaseColor.png') }, pelvis: restPelvis(f) },
    hair, clips, animPelvis, samplers: {},
  };
  for (const sex of ['m', 'f']) prepBody(LIB[sex]);
  return LIB;
}

// per-vertex clothing masks from the skin weights: cloth0 = (torso, upper arm, forearm, hips+thighs), cloth1 = (calves, feet, hands, neck)
const GROUPS = [
  [/^spine_0[123]$|^clavicle_/, 0, 0], [/^upperarm_/, 0, 1], [/^lowerarm_/, 0, 2], [/^pelvis$|^thigh_/, 0, 3],
  [/^calf_/, 1, 0], [/^foot_|^ball_/, 1, 1], [/^hand_|^(index|middle|ring|pinky|thumb)_/, 1, 2], [/^neck_01$/, 1, 3],
];
function prepBody(lib) {
  lib.gltf.scene.traverse(o => {
    if (!o.isSkinnedMesh) return;
    if (/Sphere|SuperHero/i.test(o.name) || o.geometry.attributes.position.count > 3000) lib.body = o;
  });
  const geo = lib.body.geometry, bones = lib.body.skeleton.bones;
  const si = geo.attributes.skinIndex, sw = geo.attributes.skinWeight, n = si.count;
  const c0 = new Float32Array(n * 4), c1 = new Float32Array(n * 4);
  const groupOf = bones.map(b => { for (const [re, a, k] of GROUPS) if (re.test(b.name)) return [a, k]; return null; });
  for (let i = 0; i < n; i++) for (let j = 0; j < 4; j++) {
    const g = groupOf[si.getComponent(i, j)], w = sw.getComponent(i, j);
    if (g) (g[0] ? c1 : c0)[i * 4 + g[1]] += w;
  }
  geo.setAttribute('cloth0', new THREE.BufferAttribute(c0, 4));
  geo.setAttribute('cloth1', new THREE.BufferAttribute(c1, 4));
}

// number / logo printed on a shirt, front and back
function printTex(text, color = '#ffffff') {
  const c = document.createElement('canvas'); c.width = c.height = 256;
  const g = c.getContext('2d'); g.clearRect(0, 0, 256, 256); g.fillStyle = color;
  const s = String(text); g.font = `900 ${s.length > 2 ? 110 : 170}px Helvetica, Arial`; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(s, 128, 136);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

function bodyMaterial(src, o) {
  const m = src.clone();
  const u = {
    uShirt: { value: new THREE.Color(o.shirt ?? 0xffffff) }, uShirtOn: { value: o.shirt === null || o.shirt === false ? 0 : 1 },
    uSleeve: { value: { none: 0, short: 1, long: 2 }[o.sleeves] ?? 1 },
    uPants: { value: new THREE.Color(o.pants ?? 0x2b2b35) }, uLong: { value: o.legs === 'shorts' ? 0 : 1 },
    uShoes: { value: new THREE.Color(o.shoes ?? 0x1c1c1c) }, uGloves: { value: new THREE.Color(o.gloves ?? 0xffffff) }, uGlovesOn: { value: o.gloves ? 1 : 0 },
    uSkin: { value: new THREE.Color(o.skinTint) }, uPrint: { value: o.number != null ? printTex(o.number, o.numberColor) : null }, uPrintOn: { value: o.number != null ? 1 : 0 },
    uTrim: { value: new THREE.Color(o.trim ?? o.shirt ?? 0xffffff) },
  };
  m.map = o.skinMap;
  m.userData.u = u;
  m.onBeforeCompile = (sh) => {
    Object.assign(sh.uniforms, u);
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nattribute vec4 cloth0; attribute vec4 cloth1; varying vec4 vC0; varying vec4 vC1; varying vec3 vBind;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvC0 = cloth0; vC1 = cloth1; vBind = position;');
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', `#include <common>
varying vec4 vC0; varying vec4 vC1; varying vec3 vBind;
uniform vec3 uShirt, uPants, uShoes, uSkin, uGloves, uTrim; uniform float uShirtOn, uSleeve, uLong, uPrintOn, uGlovesOn; uniform sampler2D uPrint;
float clothMask; `)
      .replace('#include <map_fragment>', `#include <map_fragment>
{
  vec3 skin = diffuseColor.rgb * uSkin;
  float lum = dot(diffuseColor.rgb, vec3(.3, .59, .11));
  float fold = .9 + .18 * smoothstep(.05, .55, lum);             // keep a little of the painted shading as cloth folds
  float shirt = uShirtOn * clamp(vC0.x + vC0.y * step(.5, uSleeve) + vC0.z * step(1.5, uSleeve), 0., 1.);
  float pants = clamp(vC0.w + vC1.x * uLong, 0., 1.);
  float shoes = clamp(vC1.y, 0., 1.);
  float gloves = uGlovesOn * clamp(vC1.z, 0., 1.);
  shirt = smoothstep(.35, .65, shirt); pants = smoothstep(.35, .65, pants); shoes = smoothstep(.3, .6, shoes); gloves = smoothstep(.3, .6, gloves);
  vec3 col = skin;
  col = mix(col, uPants * fold, pants);
  col = mix(col, uShirt * fold, shirt * (1. - step(.98, pants) * step(vBind.y, .93)));
  col = mix(col, uShoes * (.7 + .3 * fold), shoes);
  col = mix(col, uGloves * fold, gloves);
  if (uPrintOn > .5 && shirt > .5 && abs(vBind.x) < .15 && vBind.y > 1.08 && vBind.y < 1.40) {
    vec2 st = vec2((vBind.z > 0. ? vBind.x : -vBind.x) / .30 + .5, (vBind.y - 1.08) / .32);
    vec4 p = texture2D(uPrint, st);
    col = mix(col, p.rgb * fold, p.a);
  }
  clothMask = max(max(shirt, pants), max(shoes, gloves));
  diffuseColor.rgb = col;
}`)
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = mix(roughnessFactor, .9, clothMask);')
      .replace('#include <normal_fragment_maps>', '#include <normal_fragment_maps>\nnormal = normalize(mix(normal, nonPerturbedNormal, clothMask * .7));');
  };
  m.customProgramCacheKey = () => 'viral-human-v1';
  return m;
}

// ── sampling a clip at a time, straight into the bones ──────────────────────
function sampler(name) {
  const lib = LIB, clip = lib.clips[CLIPS[name] || name] || lib.clips.Idle_Loop;
  if (lib.samplers[clip.name]) return lib.samplers[clip.name];
  const tracks = [];
  for (const tr of clip.tracks) {
    const [bone, prop] = tr.name.split('.');
    if (prop === 'quaternion' || (prop === 'position' && bone === 'pelvis')) tracks.push({ bone, prop, interp: tr.createInterpolant() });
  }
  const loop = /Loop|Idle$|_Idle|Dance/.test(clip.name);
  return (lib.samplers[clip.name] = { tracks, duration: clip.duration, loop, name: clip.name });
}
const _q = new THREE.Quaternion(), _v = new THREE.Vector3();

/** pose a skeleton's bones (map name -> bone) with clip `name` at `time` (seconds), blended in by w */
function applyClip(bones, sex, name, time, w = 1) {
  const s = sampler(name);
  let t = s.loop ? ((time % s.duration) + s.duration) % s.duration : Math.max(0, Math.min(s.duration - 1e-4, time));
  const ratio = LIB[sex].pelvis.z / LIB.animPelvis.z;
  for (const tr of s.tracks) {
    const b = bones[tr.bone]; if (!b) continue;
    const r = tr.interp.evaluate(t);
    if (tr.prop === 'quaternion') { _q.fromArray(r); if (w >= 1) b.quaternion.copy(_q); else b.quaternion.slerp(_q, w); }
    else { _v.fromArray(r).multiplyScalar(ratio); if (w >= 1) b.position.copy(_v); else b.position.lerp(_v, w); }
  }
}

// turn a bone so that the direction to its child points (in world space) toward dir, by amount a (0..1)
const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _pq = new THREE.Quaternion(), _dq = new THREE.Quaternion();
function aim(bone, child, dir, a = 1) {
  bone.updateWorldMatrix(true, true);
  bone.getWorldPosition(_a); child.getWorldPosition(_b);
  const cur = _b.sub(_a).normalize();
  _dq.setFromUnitVectors(cur, _v.copy(dir).normalize());
  bone.parent.getWorldQuaternion(_pq);
  // world delta -> bone local: q_local' = parent^-1 * delta * parent * q_local
  const worldQ = _pq.clone().multiply(bone.quaternion);
  const target = _dq.multiply(worldQ);
  const local = _pq.clone().invert().multiply(target);
  bone.quaternion.slerp(local, a);
}

/**
 * Build the person kit on top of K (called by kit.js once the models are loaded).
 */
export function humanKit(K, updaters) {
  const { scene } = K;
  const UP = new THREE.Vector3(0, 1, 0);

  function dress(o) {
    const sex = o.sex === 'f' || o.sex === 'female' ? 'f' : 'm';
    const sk = typeof o.skin === 'number' ? ['light', o.skin] : (SKIN[o.skin] || SKIN[['light', 'tan', 'brown', 'dark'][Math.floor(Math.random() * 4)]]);
    return { sex, skinMap: LIB[sex].skin[sk[0]], skinTint: sk[1] };
  }

  function hairMeshes(o, sex) {
    const list = [];
    const style = o.hair === undefined ? (sex === 'f' ? 'long' : 'buzzed') : o.hair;
    const hairColor = new THREE.Color(o.hairColor ?? 0x2a1c12);
    const name = style && style !== 'bald' && style !== 'none' ? (HAIRS[style] || HAIRS.buzzed) : null;
    if (name) list.push([LIB.hair[name], hairColor]);
    if (o.beard) list.push([LIB.hair.Hair_Beard, hairColor]);
    return list;
  }

  // ── costumes: hats and helmets, capes, robes, shields and things held in the right hand ─────────────
  // Built in the idle pose and pinned to bones, so they move with every animation. Most of the user's video ideas are
  // history / myth crossovers (Napoleon, Spartans, Lincoln, Zeus, plague doctors...): a hat, a cape and a prop are
  // what make a character readable at a glance.
  const BRONZE = 0xb08d57, STEEL = 0xb9bec7, GOLD = 0xd4af37, WOOD = 0x6b4a2e;
  function costume(o, g, root, bones, sex) {
    if (!o.hat && !o.cape && !o.robe && !o.shield && !o.holding) return;
    applyClip(bones, sex, 'idle', 0); g.updateMatrixWorld(true);
    const at = (n) => root.worldToLocal(bones[n].getWorldPosition(new THREE.Vector3()));
    const pin = (bone, obj) => { root.add(obj); root.updateMatrixWorld(true); bones[bone].attach(obj); return obj; };
    const cloth = (c, x = {}) => K.mat(c, Object.assign({ roughness: .85 }, x));
    const metal = (c) => K.mat(c, { roughness: .32, metalness: .8 });
    const mesh = (geo, mat, p, r, s, parent) => { const m = new THREE.Mesh(geo, mat); if (p) m.position.set(...p); if (r) m.rotation.set(...r);
      if (s) m.scale.set(...s); parent.add(m); return m; };
    // a half disc standing upright: across the head (bicorne) or front to back (a helmet crest)
    const arch = (r, thick, flat, mat, parent, p, sideways) => mesh(new THREE.CylinderGeometry(r, r, thick, 32, 1, false, -Math.PI / 2, Math.PI), mat, p,
      [-Math.PI / 2, sideways ? Math.PI / 2 : 0, 0, ], [1, 1, flat], parent);
    if (o.hat) {
      const head = at('Head'), h = new THREE.Group(), c = o.hatColor;
      const top = head.y + (sex === 'f' ? .2 : .205), mid = -.105;       // hat space: y 0 = the top of the head
      switch (o.hat) {
        case 'bicorne': { const m = cloth(c ?? 0x15151a);                // Napoleon, worn side to side
          arch(.28, .1, .6, m, h, [0, -.07, 0], false);
          mesh(new THREE.CylinderGeometry(.04, .04, .012, 20), cloth(0x1f3fa0), [.13, .04, .055], [Math.PI / 2, 0, 0], null, h);
          mesh(new THREE.CylinderGeometry(.026, .026, .014, 20), cloth(0xf2f2f2), [.13, .04, .058], [Math.PI / 2, 0, 0], null, h);
          mesh(new THREE.CylinderGeometry(.012, .012, .016, 12), cloth(0xc0202a), [.13, .04, .06], [Math.PI / 2, 0, 0], null, h); break; }
        case 'tricorn': { const m = cloth(c ?? 0x1a1a22);                // colonial, redcoat, pirate
          mesh(new THREE.CylinderGeometry(.26, .26, .03, 3), m, [0, -.05, 0], [0, Math.PI, 0], null, h);
          mesh(new THREE.CylinderGeometry(.27, .22, .07, 3, 1, true), cloth(c ?? 0x1a1a22, { side: THREE.DoubleSide }), [0, -.02, 0], [0, Math.PI, 0], null, h);
          mesh(new THREE.CylinderGeometry(.105, .118, .1, 24), m, [0, .0, 0], null, null, h); break; }
        case 'tophat': { const m = cloth(c ?? 0x111114, { roughness: .5 });  // Lincoln
          mesh(new THREE.CylinderGeometry(.108, .1, .25, 28), m, [0, .09, 0], null, null, h);
          mesh(new THREE.CylinderGeometry(.18, .18, .014, 32), m, [0, -.03, 0], null, null, h);
          mesh(new THREE.CylinderGeometry(.109, .104, .045, 28), cloth(0x2a2a30), [0, -.002, 0], null, null, h); break; }
        case 'crown': { const m = metal(c ?? GOLD);
          mesh(new THREE.CylinderGeometry(.118, .112, .07, 28, 1, true), K.mat(c ?? GOLD, { roughness: .3, metalness: .85, side: THREE.DoubleSide }), [0, -.03, 0], null, null, h);
          for (let k = 0; k < 7; k++) { const a = k / 7 * Math.PI * 2; mesh(new THREE.ConeGeometry(.022, .075, 6), m, [Math.sin(a) * .115, .035, Math.cos(a) * .115], null, null, h);
            mesh(new THREE.SphereGeometry(.013, 8, 6), K.mat([0xc0102a, 0x1040c0, 0x10a050][k % 3], { roughness: .2 }), [Math.sin(a) * .121, -.03, Math.cos(a) * .121], null, null, h); } break; }
        case 'helmet': case 'greek': case 'viking': {                       // Roman galea / Greek crest / horned
          const m = metal(c ?? (o.hat === 'viking' ? STEEL : BRONZE));
          mesh(new THREE.SphereGeometry(.132, 28, 14, 0, Math.PI * 2, 0, Math.PI * .56), m, [0, mid + .02, -.005], null, null, h);
          for (const sx of [-1, 1]) mesh(new THREE.BoxGeometry(.018, .11, .075), m, [sx * .118, mid - .045, .035], [0, 0, sx * .12], null, h);
          mesh(new THREE.CylinderGeometry(.14, .155, .06, 24, 1, true, Math.PI * .6, Math.PI * .8), K.mat(c ?? (o.hat === 'viking' ? STEEL : BRONZE), { roughness: .32, metalness: .8, side: THREE.DoubleSide }), [0, mid - .03, -.01], null, null, h);
          if (o.hat === 'viking') for (const sx of [-1, 1]) mesh(new THREE.ConeGeometry(.03, .2, 12), cloth(0xefe6d2, { roughness: .5 }), [sx * .15, mid + .1, 0], [0, 0, -sx * .9], null, h);
          else arch(o.hat === 'greek' ? .2 : .135, .035, o.hat === 'greek' ? 1 : .8, cloth(o.crest ?? 0xb3121f), h, [0, mid + .13, -.01], true);
          break; }
        case 'knight': { const m = metal(c ?? STEEL);
          mesh(new THREE.CylinderGeometry(.135, .14, .3, 28), m, [0, mid + .01, 0], null, null, h);
          mesh(new THREE.BoxGeometry(.2, .02, .03), K.mat(0x050505), [0, mid + .02, .13], null, null, h);
          if (o.crest) arch(.12, .03, .8, cloth(o.crest), h, [0, mid + .16, 0], true); break; }
        case 'astronaut': {
          mesh(new THREE.SphereGeometry(.2, 32, 20), K.mat(0xf4f4f2, { roughness: .35 }), [0, mid - .01, 0], null, null, h);
          mesh(new THREE.SphereGeometry(.204, 32, 20, -Math.PI * .32, Math.PI * .64, Math.PI * .3, Math.PI * .34), K.mat(0xd9a52a, { roughness: .08, metalness: 1 }), [0, mid - .01, 0], null, null, h);
          mesh(new THREE.TorusGeometry(.13, .03, 10, 28), K.mat(0xc9ccd2, { metalness: .6, roughness: .4 }), [0, mid - .2, 0], [Math.PI / 2, 0, 0], null, h); break; }
        case 'laurel': { const leaf = cloth(c ?? 0x4f8a32, { roughness: .6 });
          for (let k = 0; k < 22; k++) { const a = k / 22 * Math.PI * 2; if (Math.abs(Math.sin(a / 2)) < .12) continue;
            mesh(new THREE.SphereGeometry(.03, 8, 6), leaf, [Math.sin(a) * .118, -.07, Math.cos(a) * .118], [0, a, .6], [.45, 1, .2], h); } break; }
        case 'halo':
          mesh(new THREE.TorusGeometry(.14, .013, 8, 40), K.glowMat(c ?? 0xffd76a, 2.4), [0, .09, -.02], [Math.PI / 2, 0, 0], null, h); break;
        case 'cap': { const m = cloth(c ?? 0x1d3f8a, { roughness: .7 });
          mesh(new THREE.SphereGeometry(.122, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), m, [0, mid + .055, 0], null, [1, .9, 1.04], h);
          mesh(new THREE.CylinderGeometry(.12, .12, .012, 24, 1, false, -Math.PI / 2, Math.PI), m, [0, mid + .055, .07], null, [1, 1, 1], h); break; }
        case 'cowboy': { const m = cloth(c ?? 0x7a5230, { roughness: .8 });
          mesh(new THREE.CylinderGeometry(.27, .27, .018, 32), m, [0, -.045, 0], null, [1, 1, .9], h);
          mesh(new THREE.CylinderGeometry(.1, .122, .14, 24), m, [0, .02, 0], null, [1, 1, .9], h); break; }
        case 'chef': { const m = cloth(c ?? 0xf8f8f6, { roughness: .9 });
          mesh(new THREE.CylinderGeometry(.13, .118, .2, 24), m, [0, .05, 0], null, null, h);
          mesh(new THREE.SphereGeometry(.16, 20, 12), m, [0, .17, 0], null, [1, .55, 1], h); break; }
        case 'wizard': { const m = cloth(c ?? 0x3a2a8a, { roughness: .8 });
          mesh(new THREE.ConeGeometry(.15, .46, 24), m, [0, .18, -.02], [-.12, 0, 0], null, h);
          mesh(new THREE.CylinderGeometry(.28, .28, .014, 32), m, [0, -.04, 0], null, null, h); break; }
        case 'plague': { const m = cloth(c ?? 0x121214, { roughness: .8 });   // plague doctor: wide hat and beak mask
          mesh(new THREE.CylinderGeometry(.3, .3, .016, 32), m, [0, -.04, 0], null, null, h);
          mesh(new THREE.CylinderGeometry(.11, .125, .13, 24), m, [0, .02, 0], null, null, h);
          mesh(new THREE.ConeGeometry(.055, .28, 16), cloth(0xd9ceb4, { roughness: .6 }), [0, mid - .055, .2], [Math.PI / 2, 0, 0], null, h);
          for (const sx of [-1, 1]) mesh(new THREE.SphereGeometry(.026, 12, 8), K.mat(0x202830, { roughness: .1, metalness: .4 }), [sx * .045, mid + .02, .1], null, null, h); break; }
        case 'fur': {                                                        // Mongol / steppe hat
          mesh(new THREE.ConeGeometry(.13, .2, 20), cloth(c ?? 0x8a1f1f), [0, .03, 0], null, null, h);
          mesh(new THREE.TorusGeometry(.12, .042, 10, 28), cloth(0x6b4a2e, { roughness: 1 }), [0, -.06, 0], [Math.PI / 2, 0, 0], null, h); break; }
        case 'pharaoh': {                                                    // nemes headdress
          const m = cloth(c ?? 0x1f4fa0, { roughness: .6 });
          mesh(new THREE.SphereGeometry(.135, 24, 12, 0, Math.PI * 2, 0, Math.PI * .6), m, [0, mid + .03, -.01], null, null, h);
          for (const sx of [-1, 1]) mesh(new THREE.BoxGeometry(.07, .26, .05), m, [sx * .12, mid - .14, .02], [0, 0, sx * .08], null, h);
          mesh(new THREE.TorusGeometry(.13, .012, 8, 28), metal(GOLD), [0, mid + .03, 0], [Math.PI / 2, 0, 0], null, h); break; }
        default: break;
      }
      h.position.set(head.x, top, head.z + .01);
      pin('Head', h);
    }
    if (o.cape) {                                                          // from the shoulders to mid-calf, behind
      const s3 = at('spine_03'), len = s3.y - .2;
      const geo = new THREE.CylinderGeometry(.24, .44, len, 20, 6, true, Math.PI / 2 + .3, Math.PI - .6); geo.translate(0, -len / 2, 0);
      const cape = new THREE.Mesh(geo, cloth(o.cape === true ? 0xa3121f : o.cape, { side: THREE.DoubleSide }));
      cape.scale.z = .64; cape.position.set(s3.x, s3.y + .16, s3.z + .045); cape.rotation.x = .07;
      pin('spine_03', cape);
    }
    if (o.robe) {                                                          // toga, robe, long coat: waist to ankles
      const pel = at('pelvis'), topY = pel.y + .09, len = topY - .05, col = o.robe === true ? 0xf1ede2 : o.robe;
      const geo = new THREE.CylinderGeometry(.158, .32, len, 28, 4, true); geo.translate(0, -len / 2, 0);
      const robe = new THREE.Mesh(geo, cloth(col, { side: THREE.DoubleSide }));
      robe.scale.z = .8; robe.position.set(pel.x, topY, pel.z + .075);
      const belt = new THREE.Mesh(new THREE.TorusGeometry(.158, .022, 8, 28), cloth(o.belt ?? new THREE.Color(col).multiplyScalar(.55).getHex(), { roughness: .6 }));
      belt.rotation.x = Math.PI / 2; belt.scale.set(1, .8, 1); belt.position.set(pel.x, topY - .01, pel.z + .075);
      pin('pelvis', robe); pin('pelvis', belt);
    }
    if (o.hat === 'astronaut') {                                           // the life-support pack sells the suit
      const s3 = at('spine_03'), pack = new THREE.Group();
      mesh(new THREE.BoxGeometry(.4, .52, .2), K.mat(0xeeeeec, { roughness: .5 }), null, null, null, pack);
      mesh(new THREE.BoxGeometry(.3, .12, .03), K.mat(0x8a8f98, { roughness: .4, metalness: .5 }), [0, .12, -.105], null, null, pack);
      pack.position.set(s3.x, s3.y - .02, s3.z - .2);
      pin('spine_03', pack);
    }
    if (o.shield) {                                                        // on the left forearm, facing out and forward
      const la = at('lowerarm_l'), hl = at('hand_l'), sh = new THREE.Group();
      if (o.shield === 'scutum') {                                         // Roman: a tall curved red board
        mesh(new THREE.CylinderGeometry(.6, .6, 1.05, 18, 1, true, -.45, .9), cloth(o.shieldColor ?? 0x9b1b1b, { roughness: .6, side: THREE.DoubleSide }), [0, 0, -.55], null, null, sh);
        mesh(new THREE.SphereGeometry(.07, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2), metal(GOLD), [0, 0, .05], [Math.PI / 2, 0, 0], null, sh);
      } else {                                                             // round Greek hoplon
        mesh(new THREE.CylinderGeometry(.42, .42, .04, 40), metal(o.shieldColor ?? BRONZE), null, [Math.PI / 2, 0, 0], null, sh);
        mesh(new THREE.TorusGeometry(.42, .025, 8, 40), metal(0x8a6a3a), null, null, null, sh);
        if (o.emblem !== false) mesh(new THREE.CylinderGeometry(.16, .16, .045, 3), cloth(o.emblemColor ?? 0x9b1b1b), [0, 0, .005], [Math.PI / 2, 0, Math.PI], null, sh);
      }
      sh.position.copy(la.clone().lerp(hl, .5)).add(new THREE.Vector3(.1, 0, .05));
      sh.rotation.y = Math.atan2(1, .5);
      pin('lowerarm_l', sh);
    }
    if (o.holding) {                                                       // in the right hand, upright at rest
      const hr = at('hand_r'), it = new THREE.Group(), wood = cloth(WOOD, { roughness: .7 });
      switch (o.holding) {
        case 'spear': mesh(new THREE.CylinderGeometry(.017, .017, 2.3, 8), wood, [0, .23, 0], null, null, it);
          mesh(new THREE.ConeGeometry(.04, .22, 8), metal(BRONZE), [0, 1.49, 0], null, null, it); break;
        case 'staff': mesh(new THREE.CylinderGeometry(.022, .028, 1.8, 8), wood, [0, .2, 0], null, null, it);
          mesh(new THREE.SphereGeometry(.045, 12, 8), wood, [0, 1.12, 0], null, null, it); break;
        case 'trident': { const m = metal(o.holdColor ?? GOLD);
          mesh(new THREE.CylinderGeometry(.018, .018, 2.0, 8), m, [0, .2, 0], null, null, it);
          mesh(new THREE.BoxGeometry(.26, .03, .03), m, [0, 1.2, 0], null, null, it);
          for (const x of [-.12, 0, .12]) mesh(new THREE.ConeGeometry(.022, .28, 8), m, [x, 1.35, 0], null, null, it); break; }
        case 'torch': mesh(new THREE.CylinderGeometry(.025, .02, .55, 8), wood, [0, .15, 0], null, null, it);
          mesh(new THREE.ConeGeometry(.06, .2, 12), K.glowMat(0xff8a2a, 2.6), [0, .5, 0], null, null, it);
          mesh(new THREE.ConeGeometry(.035, .14, 10), K.glowMat(0xffe08a, 3), [0, .47, 0], null, null, it); break;
        case 'flag': { mesh(new THREE.CylinderGeometry(.016, .016, 2.1, 8), wood, [0, .4, 0], null, null, it);
          const cols = [].concat(o.flagColor ?? [0x1f3fa0, 0xf2f2f2, 0xc0202a]);
          cols.forEach((col, k) => mesh(new THREE.PlaneGeometry(.95 / cols.length, .6), cloth(col, { side: THREE.DoubleSide }), [.03 + (k + .5) * .95 / cols.length, 1.15, 0], null, null, it)); break; }
        case 'sword': mesh(new THREE.BoxGeometry(.05, .85, .012), metal(STEEL), [0, 0, .0], null, null, it);
          mesh(new THREE.BoxGeometry(.2, .03, .04), metal(GOLD), [0, -.43, 0], null, null, it);
          mesh(new THREE.CylinderGeometry(.018, .018, .16, 8), cloth(0x3a2a1a), [0, -.52, 0], null, null, it);
          it.children.forEach(m => m.position.y += .9); it.rotation.x = Math.PI * .5; break;
        case 'hotdog': mesh(new THREE.CapsuleGeometry(.032, .14, 4, 10), cloth(0xd9a35a, { roughness: .9 }), null, null, [1.25, 1, 1.05], it);
          mesh(new THREE.CapsuleGeometry(.02, .17, 4, 10), cloth(0xa0402a, { roughness: .5 }), [0, 0, .02], null, null, it);
          mesh(new THREE.BoxGeometry(.012, .15, .006), cloth(0xf0c020), [0, 0, .042], null, null, it);
          it.rotation.x = Math.PI / 2; break;
        case 'mic': mesh(new THREE.CylinderGeometry(.016, .012, .18, 10), cloth(0x222222), null, null, null, it);
          mesh(new THREE.SphereGeometry(.035, 12, 10), metal(0x9a9a9a), [0, .1, 0], null, null, it); break;
        case 'scroll': mesh(new THREE.CylinderGeometry(.03, .03, .3, 14), cloth(0xe8dcb8), null, [0, 0, Math.PI / 2], null, it); break;
        default: break;
      }
      it.position.copy(hr).add(new THREE.Vector3(0, -.06, .035));
      pin('hand_r', it);
    }
  }

  /**
   * A realistic person (1.8 m, faces +z). o: sex 'm'|'f', skin 'light'|'pale'|'tan'|'brown'|'dark'|'red'|'devil'|'green'|hex,
   * hair 'buzzed'|'parted'|'long'|'buns'|'bald', hairColor, beard, shirt (hex, or null = shirtless), sleeves 'none'|'short'|'long',
   * pants hex, legs 'shorts'|'long', shoes hex, gloves hex (boxing), number (jersey text), numberColor, horns, eyes (glow hex),
   * s (scale; 1.1 = a 2 m athlete, 1.3 = a giant), rot.
   * p.play(clip, time, w) poses it (see CLIPS); p.walk(t, speed) / p.run(t) / p.pose(name, x) as shortcuts;
   * p.aimArm('l'|'r', [x,y,z] world dir, amount) points an arm; p.bone(name) / p.handPos('r') for attaching things.
   */
  K.person = (pos, o = {}) => {
    const { sex, skinMap, skinTint } = dress(o);
    const lib = LIB[sex];
    const root = SkeletonUtils.clone(lib.gltf.scene);
    const g = new THREE.Group(); g.add(root);
    g.scale.setScalar(o.s ?? 1); g.rotation.y = o.rot ?? 0;
    let body = null, eyes = null, skeleton = null;
    root.traverse(m => {
      if (!m.isSkinnedMesh) return;
      if (m.geometry === lib.body.geometry) body = m;
      else if (/Eye/.test(m.material.name)) eyes = m;
      m.frustumCulled = false;
    });
    skeleton = body.skeleton;
    // under a robe the legs wear its colour, so a stride poking through the cloth doesn't show dark trousers
    const robeLegs = o.robe && o.pants == null ? { pants: o.robe === true ? 0xf1ede2 : o.robe, legs: 'long' } : {};
    body.material = bodyMaterial(lib.body.material, Object.assign({}, o, robeLegs, { skinMap, skinTint }));
    if (o.eyes && eyes) eyes.material = K.glowMat(o.eyes, 6);
    root.traverse(m => { if (m.isSkinnedMesh && m !== body && m !== eyes) { m.material = m.material.clone(); m.material.color = new THREE.Color(o.hairColor ?? 0x2a1c12).multiplyScalar(2.2); } });
    for (const [src, color] of hairMeshes(o, sex)) {
      const h = new THREE.SkinnedMesh(src.geometry, src.material.clone());
      h.material.color = color.clone().multiplyScalar(2.2); h.material.side = THREE.DoubleSide; h.material.alphaTest = .4;
      h.frustumCulled = false; h.bind(skeleton, body.bindMatrix); body.parent.add(h);
    }
    const bones = {}; for (const b of skeleton.bones) bones[b.name] = b;
    // (no per-bone bulk: non-uniform bone scale shears the whole arm chain; these bodies are athletic already)
    if (o.horns) {
      g.updateMatrixWorld(true);
      for (const sx of [-1, 1]) {
        const h = new THREE.Mesh(new THREE.ConeGeometry(.032, .2, 12), K.mat(0x1a0a08, { roughness: .4 }));
        h.position.set(sx * .065, 1.86, .02); h.rotation.z = -sx * .5; h.rotation.x = -.25;
        root.add(h); root.updateMatrixWorld(true); bones.Head.attach(h);
      }
    }
    if (o.shoes !== null) {            // real sneakers over the painted feet
      g.updateMatrixWorld(true);
      const shoeMat = K.mat(o.shoes ?? 0x1c1c1c, { roughness: .55 }), sole = K.mat(0xf2f2f2, { roughness: .8 });
      for (const sd of ['l', 'r']) {
        const a = bones['foot_' + sd].getWorldPosition(new THREE.Vector3()), b = bones['ball_leaf_' + sd].getWorldPosition(new THREE.Vector3());
        root.worldToLocal(a); root.worldToLocal(b);
        const len = Math.hypot(b.x - a.x, b.z - a.z) + .1, sh = new THREE.Group();
        const up = new THREE.Mesh(new THREE.SphereGeometry(.5, 20, 12), shoeMat); up.scale.set(.115, .11, len); up.position.set(0, .055, 0); sh.add(up);
        const so = new THREE.Mesh(new THREE.CylinderGeometry(.5, .5, 1, 20), sole); so.scale.set(.118, .03, len * 1.02); so.position.set(0, .016, 0); sh.add(so);
        sh.position.set((a.x + b.x) / 2, 0, (a.z + b.z) / 2 - .01);
        sh.rotation.y = Math.atan2(b.x - a.x, b.z - a.z);
        root.add(sh); root.updateMatrixWorld(true); bones['foot_' + sd].attach(sh);
      }
    }
    if (o.legs === 'shorts' && o.baggy !== false) {     // loose basketball / boxing shorts over the painted ones
      g.updateMatrixWorld(true);
      const mat = K.mat(o.pants ?? 0x2b2b35, { roughness: .75, side: THREE.DoubleSide });
      const pel = bones.pelvis.getWorldPosition(new THREE.Vector3()); root.worldToLocal(pel);
      const hy = pel.y, sh = new THREE.Mesh(new THREE.CylinderGeometry(.175, .215, .36, 28, 1, true), mat);
      sh.scale.z = .78; sh.position.set(0, hy - .2, .005);
      root.add(sh); root.updateMatrixWorld(true); bones.pelvis.attach(sh);
    }
    costume(o, g, root, bones, sex);
    K.add(g, pos);
    g.traverse(m => { if (m.isMesh) { m.castShadow = o.shadow !== false; m.receiveShadow = true; } });
    g.userData.sex = sex; g.bones = bones; g.skeleton = skeleton;
    g.parts = { head: bones.Head, body: bones.spine_02, armL: bones.upperarm_l, armR: bones.upperarm_r, legL: bones.thigh_l, legR: bones.thigh_r };
    g.play = (name, t = 0, w = 1) => { applyClip(bones, sex, name, t, w); return g; };
    g.walk = (t, speed = 1) => g.play('walk', t * speed);
    g.run = (t, speed = 1) => g.play('sprint', t * speed);
    g.bone = (n) => bones[n];
    g.handPos = (side = 'r') => bones['hand_' + side].getWorldPosition(new THREE.Vector3());
    const _gq = new THREE.Quaternion();
    // dir is in the person's own space: +x = their left, +y = up, +z = where they face
    g.aimArm = (side, dir, a = 1) => { g.getWorldQuaternion(_gq); dir = K.v3(dir).applyQuaternion(_gq); aim(bones['upperarm_' + side], bones['lowerarm_' + side], K.v3(dir), a); aim(bones['lowerarm_' + side], bones['hand_' + side], K.v3(dir), a); return g; };
    g.pose = (name, x = 1) => {
      x = Math.max(0, Math.min(1, x));
      const P = {
        idle: () => g.play('idle', x * 2.5), punch: () => g.play('punch', x), jab: () => g.play('jab', x * .87),
        fight: () => g.play('jab', 0), fall: () => g.play('death', x * 2.4), hit: () => g.play('hit_chest', x * .33),
        jump: () => x < .3 ? g.play('jump_start', x / .3 * 1.33) : x < .8 ? g.play('jump_air', (x - .3) * 5) : g.play('jump_land', (x - .8) * 6),
        arms_up: () => { g.play('idle', x); g.aimArm('l', [.35, 1, .1], .9); g.aimArm('r', [-.35, 1, .1], .9); },
        cheer: () => { g.play('idle', x); const k = .75 + .25 * Math.sin(x * 40); g.aimArm('l', [.4, 1, .2], k); g.aimArm('r', [-.4, 1, .2], k); },
        dunk: () => { g.play('jump_air', 1); g.aimArm('r', [0, 1, .25], 1); g.aimArm('l', [.3, .8, .2], .7); },
        shoot: () => { g.play('idle', 0); const k = Math.min(1, x * 1.6); g.aimArm('r', [0, 1, .35], k); g.aimArm('l', [.1, 1, .45], k * .85); },
        dribble: () => { g.play('jog', x * .93); const d = Math.abs(Math.sin(x * Math.PI * 4)); g.aimArm('r', [-.25, -1 + d * .6, .55], .8); },
        sit: () => g.play('sit', x), dance: () => g.play('dance', x), cast: () => g.play('cast', x * .5), point: () => { g.play('idle', 0); g.aimArm('r', [0, .1, 1], x); },
      };
      (P[name] || (() => g.play(name, x)))();
      return g;
    };
    g.play('idle', 0);
    return g;
  };

  /**
   * a crowd of `count` people over the box [x0,z0]-[x1,z1]. o.walk: they stroll along ±z (and wrap around);
   * o.face: [x,z] they all look at (spectators). people.cheer(on, intensity) makes spectators jump and wave.
   * Spectators share a few animated skeletons, so hundreds stay cheap.
   */
  const SHIRTS = [0x3a6ea5, 0xc0392b, 0x2e8b57, 0xe0b23a, 0x8e44ad, 0xecf0f1, 0x34495e, 0xd35400, 0x16a085, 0x7f8c8d, 0xe84393, 0x111111, 0xf5f5f5];
  const PANTS = [0x2b2b35, 0x1f3a5a, 0x4a3b2a, 0x111111, 0x6b6b6b, 0xc2b280, 0x3b4a6b];
  const TONES = ['light', 'pale', 'tan', 'brown', 'dark', 'brown', 'tan'];
  // spectators everywhere share 12 animated skeletons (6 per sex); each spectator's meshes are detached-bound to one
  let puppets = null;
  const cheer = { on: 0, from: -1e9 };
  function pool() {
    if (puppets) return puppets;
    puppets = [];
    for (const sex of ['m', 'f']) for (let k = 0; k < 6; k++) {
      const root = SkeletonUtils.clone(LIB[sex].gltf.scene); let body = null;
      root.traverse(m => { if (m.isSkinnedMesh && m.geometry === LIB[sex].body.geometry) body = m; });
      const bones = {}; for (const bn of body.skeleton.bones) bones[bn.name] = bn;
      root.updateMatrixWorld(true);
      const pp = { sex, root, skeleton: body.skeleton, bones, ph: k * 1.37, mode: k % 3 };
      pp.aimArm = (side, dir, amt) => { aim(bones['upperarm_' + side], bones['lowerarm_' + side], K.v3(dir), amt); aim(bones['lowerarm_' + side], bones['hand_' + side], K.v3(dir), amt); };
      puppets.push(pp);
    }
    updaters.push((t) => {
      cheerAt(t);
      for (const pp of puppets) {
        if (cheer.on) {
          const x = t * (1.1 + pp.mode * .15) + pp.ph;
          applyClip(pp.bones, pp.sex, 'idle', x);
          const k = (.7 + .3 * Math.sin(x * 9)) * Math.min(1, Math.max(0, t - cheer.from) * 3) * cheer.on;
          if (pp.mode === 0) { pp.aimArm('l', [.4, 1, .25], k); pp.aimArm('r', [-.4, 1, .25], k); }
          else if (pp.mode === 1) pp.aimArm('r', [-.3, 1, .3], k);
          else { pp.aimArm('l', [.6, .7, .4], k); pp.aimArm('r', [-.6, .7, .4], k); }
          pp.bones.pelvis.position.z += Math.abs(Math.sin(x * (6 + pp.mode))) * .07 * k;
        } else applyClip(pp.bones, pp.sex, pp.mode === 2 ? 'talk' : 'idle', t + pp.ph);
        pp.root.updateMatrixWorld(true);
      }
    });
    return puppets;
  }
  /** spectators (every crowd) cheer from time t0: on = true/false or an intensity 0..1 */
  // calls stack on a timeline (K.cheer(true, 10); K.cheer(false, 14) = cheer from 10 s to 14 s)
  const cheerPlan = []; let cheerNow = 0;
  K.cheer = (on = true, t0 = cheerNow) => {
    const v = on === true ? 1 : (+on || 0), last = cheerPlan[cheerPlan.length - 1];
    if (last && last.on === v && last.t0 <= t0) return;
    cheerPlan.push({ on: v, t0 }); cheerPlan.sort((a, b) => a.t0 - b.t0);
  };
  function cheerAt(t) {
    cheerNow = t; cheer.on = 0; cheer.from = -1e9;
    for (const c of cheerPlan) if (c.t0 <= t) { cheer.on = c.on; cheer.from = c.t0; }
    return cheer;
  }
  K.cheerLevel = (t) => { cheerAt(t); return cheer.on * Math.min(1, Math.max(0, t - cheer.from) * 3); };
  K.crowd = (a, b, count = 30, o = {}) => {
    const { walk = true, speed = 1.3, face = null, s = 1 } = o;
    const people = [];
    if (walk) {
      for (let i = 0; i < count; i++) {
        const sex = Math.random() < .5 ? 'm' : 'f';
        const p = K.person([K.lerp(a[0], b[0], Math.random()), 0, K.lerp(a[1], b[1], Math.random())], {
          sex, skin: TONES[i % TONES.length], shirt: SHIRTS[(i * 3) % SHIRTS.length], sleeves: ['short', 'long', 'short'][i % 3],
          pants: PANTS[(i * 5) % PANTS.length], hair: sex === 'f' ? ['long', 'buns', 'parted'][i % 3] : ['buzzed', 'parted', 'bald', 'buzzed'][i % 4],
          hairColor: [0x2a1c12, 0x111111, 0x6b4a2a, 0xc9a35a][i % 4], beard: sex === 'm' && i % 5 === 0, s: s * (.93 + Math.random() * .12),
          shadow: true });
        p.userData.dir = Math.random() < .5 ? 1 : -1; p.userData.v = speed * (.8 + Math.random() * .4); p.userData.ph = Math.random() * 10;
        p.rotation.y = p.userData.dir > 0 ? 0 : Math.PI;
        people.push(p);
      }
      const z0 = Math.min(a[1], b[1]), z1 = Math.max(a[1], b[1]);
      updaters.push((t) => { for (const p of people) {
        if (p.userData.frozen) continue;
        p.position.z += p.userData.dir * p.userData.v * (1 / 30);
        if (p.position.z > z1) p.position.z = z0; if (p.position.z < z0) p.position.z = z1;
        p.walk(t + p.userData.ph, p.userData.v / 1.3);
      } });
      people.cheer = () => {};
      return people;
    }
    const puppets = pool();
    for (let i = 0; i < count; i++) {
      const sex = Math.random() < .55 ? 'm' : 'f';
      const pup = puppets[(sex === 'm' ? 0 : 6) + (i % 6)];
      const { skinMap, skinTint } = dress({ sex, skin: TONES[i % TONES.length] });
      const lib = LIB[sex];
      const g = new THREE.Group();
      const mesh = new THREE.SkinnedMesh(lib.body.geometry, bodyMaterial(lib.body.material, { skinMap, skinTint,
        shirt: (o.shirts || SHIRTS)[(i * 7) % (o.shirts || SHIRTS).length], sleeves: i % 3 ? 'short' : 'long', pants: PANTS[(i * 3) % PANTS.length], number: o.jerseys && i % 4 === 0 ? o.jerseys : null }));
      mesh.bindMode = THREE.DetachedBindMode; mesh.bind(pup.skeleton, new THREE.Matrix4()); mesh.frustumCulled = false; g.add(mesh);
      for (const [src, color] of hairMeshes({ hair: sex === 'f' ? ['long', 'buns', 'parted'][i % 3] : ['buzzed', 'parted', 'bald', 'buzzed'][i % 4],
                                              hairColor: [0x2a1c12, 0x111111, 0x6b4a2a, 0xc9a35a][i % 4] }, sex)) {
        const h = new THREE.SkinnedMesh(src.geometry, src.material.clone()); h.material.color = color.clone().multiplyScalar(2.2); h.material.alphaTest = .4;
        h.bindMode = THREE.DetachedBindMode; h.bind(pup.skeleton, new THREE.Matrix4()); h.frustumCulled = false; g.add(h);
      }
      g.position.set(K.lerp(a[0], b[0], Math.random()), 0, K.lerp(a[1], b[1], Math.random()));
      g.scale.setScalar(s * (.93 + Math.random() * .12));
      if (face) { g.lookAt(face[0], 0, face[1]); }
      g.rotation.y += (Math.random() - .5) * .5;
      g.traverse(m => { if (m.isMesh) { m.castShadow = false; m.receiveShadow = true; } });
      scene.add(g); g.userData.puppet = pup; people.push(g);
    }
    people.cheer = K.cheer;
    for (const p of people) { p.userData.cheer = false; }
    return people;
  };
}
