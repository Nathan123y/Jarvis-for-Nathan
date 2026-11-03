// Animated dinosaurs for the viral videos: Quaternius "Animated LowPoly Dinosaurs" (CC0), loaded once at boot.
// K.dino(kind, pos, o) clones one; d.play(clip, t) poses it at time t (deterministic: mixer.setTime).
import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import * as SkeletonUtils from 'three/addons/utils/SkeletonUtils.js';

const KINDS = { trex: ['Trex', 4.2], raptor: ['Velociraptor', 1.7], triceratops: ['Triceratops', 3.0],
                stegosaurus: ['Stegosaurus', 4.0], apatosaurus: ['Apatosaurus', 9.0], parasaurolophus: ['Parasaurolophus', 4.2] };
let LIB = null;
const PALETTE = { Green: 0x4c7a34, LightGreen: 0x93ad5c, Black: 0x141414, Red: 0xa8261c, LightYellow: 0xd8c483,
                  Brown: 0x6e4b2c, LightBrown: 0xae8a5e, DarkBrown: 0x3f2b1a, Purple: 0x5e4f7e, Material: 0x8f7d5c };

export async function loadDinos() {
  if (LIB) return LIB;
  const loader = new FBXLoader().setPath('./dinos/');
  LIB = {};
  await Promise.all(Object.entries(KINDS).map(async ([k, [file, h]]) => {
    try {
      const obj = await loader.loadAsync(file + '.fbx');
      const box = new THREE.Box3().setFromObject(obj), size = box.getSize(new THREE.Vector3());
      LIB[k] = { obj, clips: obj.animations, scale: h / size.y, height: h };
    } catch (e) { console.warn('dino', k, e); }
  }));
  return LIB;
}

export function dinoKit(K) {
  /**
   * A dinosaur (faces +z). kind: trex | raptor | triceratops | stegosaurus | apatosaurus | parasaurolophus.
   * o: s (size multiplier on a real-ish height), rot, color (tint hex). d.clips = clip names;
   * d.play(name or index, t) poses it at time t (loops wrap); names look like 'Idle', 'Walk', 'Run', 'Attack', 'Jump', 'Death'.
   */
  K.dino = (kind = 'trex', pos = [0, 0, 0], o = {}) => {
    const L = LIB && (LIB[kind] || LIB.trex);
    const g = new THREE.Group();
    if (!L) return K.add(g, pos);
    const m = SkeletonUtils.clone(L.obj);
    m.scale.setScalar(L.scale * (o.s ?? 1));
    m.traverse(c => {
      if (!c.isMesh) return;
      c.frustumCulled = false;
      const src = Array.isArray(c.material) ? c.material : [c.material];
      const out = src.map(mt => {
        // the FBX colours load far too dark; use the pack's own colour names with real, natural tones
        const col = new THREE.Color(PALETTE[mt.name] ?? 0x8a7a5a);
        if (o.color) col.multiply(new THREE.Color(o.color));
        const vc = !!c.geometry.attributes.color;
        return new THREE.MeshStandardMaterial({ color: vc ? new THREE.Color(o.color ?? 0xffffff) : col, vertexColors: vc,
                                                roughness: .8, metalness: 0, flatShading: true, map: mt.map || null });
      });
      c.material = Array.isArray(c.material) ? out : out[0];
    });
    g.add(m); g.rotation.y = o.rot ?? 0;
    const mixer = new THREE.AnimationMixer(m);
    const clips = L.clips;
    g.clips = clips.map(c => c.name);
    let cur = null;
    const find = (name) => typeof name === 'number' ? clips[name] :
      clips.find(c => c.name.toLowerCase() === String(name).toLowerCase()) ||
      clips.find(c => c.name.toLowerCase().includes(String(name).toLowerCase())) || clips[0];
    g.play = (name, t = 0) => {
      const c = find(name); if (!c) return g;
      const a = mixer.clipAction(c);
      if (cur !== a) { mixer.stopAllAction(); a.play(); cur = a; }
      mixer.setTime(Math.max(0, t));
      return g;
    };
    g.height = L.height * (o.s ?? 1);
    g.play('Idle', 0);
    return K.add(g, pos);
  };
}
