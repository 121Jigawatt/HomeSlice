import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export interface Viewer {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  render: () => void;
  frame: (box: THREE.Box3) => void;
  reset: () => void;
  dispose: () => void;
}

export const ACCENT = 0x14b8a6;
const GRID = 0x24314a;
const EDGE = 0x3b4b68;

/** Z-up scene (printer coordinates) rendered on demand, with orbit/zoom and keyboard controls. */
export function createViewer(host: HTMLElement): Viewer {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  host.appendChild(renderer.domElement);
  renderer.domElement.setAttribute("aria-hidden", "true");

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xe6edf7, 0x0b1220, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(1, -1.5, 2);
  scene.add(sun);

  const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 20000);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.screenSpacePanning = true;

  let home = { position: new THREE.Vector3(250, -350, 300), target: new THREE.Vector3(0, 0, 0) };
  let frameId = 0;
  const render = () => {
    cancelAnimationFrame(frameId);
    frameId = requestAnimationFrame(() => renderer.render(scene, camera));
  };
  controls.addEventListener("change", render);

  const frame = (box: THREE.Box3) => {
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.length() / 2, 10);
    const dist = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2));
    const dir = new THREE.Vector3(0.6, -1, 0.75).normalize();
    home = { position: center.clone().add(dir.multiplyScalar(dist)), target: center };
    camera.near = Math.max(dist / 100, 0.1);
    camera.far = dist * 20;
    reset();
  };

  const reset = () => {
    camera.position.copy(home.position);
    controls.target.copy(home.target);
    camera.updateProjectionMatrix();
    controls.update();
    render();
  };

  const resize = () => {
    const { clientWidth: w, clientHeight: h } = host;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    render();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  resize();

  // Keyboard: arrows orbit, +/- zoom, R resets (host must be focusable).
  const onKey = (e: KeyboardEvent) => {
    const step = THREE.MathUtils.degToRad(10);
    const offset = camera.position.clone().sub(controls.target);
    const spherical = new THREE.Spherical().setFromVector3(new THREE.Vector3(offset.x, offset.z, -offset.y));
    let handled = true;
    if (e.key === "ArrowLeft") spherical.theta -= step;
    else if (e.key === "ArrowRight") spherical.theta += step;
    else if (e.key === "ArrowUp") spherical.phi = Math.max(0.05, spherical.phi - step);
    else if (e.key === "ArrowDown") spherical.phi = Math.min(Math.PI - 0.05, spherical.phi + step);
    else if (e.key === "+" || e.key === "=") spherical.radius *= 0.85;
    else if (e.key === "-" || e.key === "_") spherical.radius /= 0.85;
    else if (e.key === "r" || e.key === "R") {
      reset();
      e.preventDefault();
      return;
    } else handled = false;
    if (!handled) return;
    e.preventDefault();
    const v = new THREE.Vector3().setFromSpherical(spherical);
    camera.position.copy(controls.target).add(new THREE.Vector3(v.x, -v.z, v.y));
    controls.update();
    render();
  };
  host.addEventListener("keydown", onKey);

  return {
    scene,
    camera,
    controls,
    render,
    frame,
    reset,
    dispose: () => {
      cancelAnimationFrame(frameId);
      observer.disconnect();
      host.removeEventListener("keydown", onKey);
      controls.dispose();
      scene.traverse((o) => {
        const mesh = o as THREE.Mesh;
        mesh.geometry?.dispose?.();
        const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
        (Array.isArray(mat) ? mat : mat ? [mat] : []).forEach((m) => m.dispose());
      });
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}

/** Flat build plate from Orca's printable_area polygon, with a 10 mm grid and build-volume outline. */
export function buildPlate(area: [number, number][], height: number): THREE.Group {
  const group = new THREE.Group();
  const shape = new THREE.Shape(area.map(([x, y]) => new THREE.Vector2(x, y)));
  const plate = new THREE.Mesh(
    new THREE.ShapeGeometry(shape),
    new THREE.MeshBasicMaterial({ color: 0x131c2e, side: THREE.DoubleSide }),
  );
  plate.position.z = -0.05;
  group.add(plate);

  const xs = area.map((p) => p[0]);
  const ys = area.map((p) => p[1]);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const grid: number[] = [];
  for (let x = Math.ceil(minX / 10) * 10; x <= maxX; x += 10) grid.push(x, minY, 0, x, maxY, 0);
  for (let y = Math.ceil(minY / 10) * 10; y <= maxY; y += 10) grid.push(minX, y, 0, maxX, y, 0);
  const gridGeo = new THREE.BufferGeometry();
  gridGeo.setAttribute("position", new THREE.Float32BufferAttribute(grid, 3));
  group.add(new THREE.LineSegments(gridGeo, new THREE.LineBasicMaterial({ color: GRID })));

  const outline: number[] = [];
  area.forEach(([x, y], i) => {
    const [nx, ny] = area[(i + 1) % area.length];
    outline.push(x, y, 0, nx, ny, 0, x, y, height, nx, ny, height, x, y, 0, x, y, height);
  });
  const edgeGeo = new THREE.BufferGeometry();
  edgeGeo.setAttribute("position", new THREE.Float32BufferAttribute(outline, 3));
  group.add(new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial({ color: EDGE, transparent: true, opacity: 0.6 })));
  return group;
}

export function plateBox(area: [number, number][], height: number): THREE.Box3 {
  const box = new THREE.Box3();
  area.forEach(([x, y]) => box.expandByPoint(new THREE.Vector3(x, y, 0)));
  box.expandByPoint(new THREE.Vector3(box.min.x, box.min.y, Math.min(height, 60)));
  return box;
}
