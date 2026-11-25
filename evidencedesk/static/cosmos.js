// Ambient background: a slow, flowing aurora (PS5-style light ribbons) in deep indigo / violet / blue,
// a fine star field with depth parallax, and a soft light that follows the pointer.
// Rendered with three.js (vendored locally in static/vendor) at reduced resolution: soft, smooth and cheap.
import * as THREE from "./vendor/three.module.min.js";

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const canvas = document.getElementById("cosmos");
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false, powerPreference: "low-power" });
} catch (e) { canvas.remove(); throw e; } // no WebGL: the CSS gradient behind the canvas remains
const RES = 0.6; // render scale: the aurora is soft by design, so this costs no visible quality
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2) * RES);

// ---------------------------------------------------------------- aurora (full-screen shader)
const aurora = new THREE.ShaderMaterial({
  uniforms: { uTime: { value: 0 }, uRes: { value: new THREE.Vector2(1, 1) }, uMouse: { value: new THREE.Vector2(0.5, 0.5) },
    uScroll: { value: 0 }, uPulse: { value: 0 } },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }`,
  fragmentShader: `
    precision highp float;
    varying vec2 vUv; uniform float uTime; uniform vec2 uRes; uniform vec2 uMouse; uniform float uScroll; uniform float uPulse;
    vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;} vec2 mod289(vec2 x){return x-floor(x*(1./289.))*289.;}
    vec3 permute(vec3 x){return mod289(((x*34.)+1.)*x);}
    float snoise(vec2 v){const vec4 C=vec4(.211324865405187,.366025403784439,-.577350269189626,.024390243902439);
      vec2 i=floor(v+dot(v,C.yy)); vec2 x0=v-i+dot(i,C.xx); vec2 i1=(x0.x>x0.y)?vec2(1.,0.):vec2(0.,1.);
      vec4 x12=x0.xyxy+C.xxzz; x12.xy-=i1; i=mod289(i); vec3 p=permute(permute(i.y+vec3(0.,i1.y,1.))+i.x+vec3(0.,i1.x,1.));
      vec3 m=max(.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.); m=m*m; m=m*m;
      vec3 x=2.*fract(p*C.www)-1.; vec3 h=abs(x)-.5; vec3 ox=floor(x+.5); vec3 a0=x-ox;
      m*=1.79284291400159-.85373472095314*(a0*a0+h*h); vec3 g; g.x=a0.x*x0.x+h.x*x0.y; g.yz=a0.yz*x12.xz+h.yz*x12.yw;
      return 130.*dot(m,g);}
    float fbm(vec2 p){ float s=0., a=.5; for(int i=0;i<3;i++){ s+=a*snoise(p); p*=2.02; a*=.5; } return s; }
    void main(){
      vec2 uv = vUv; float asp = uRes.x / uRes.y; vec2 p = vec2((uv.x - .5) * asp, uv.y - .5);
      float t = uTime * .035;
      vec2 m = vec2((uMouse.x - .5) * asp, uMouse.y - .5);
      vec3 col = mix(vec3(.012,.016,.045), vec3(.035,.035,.10), smoothstep(-.7,.6,p.y));
      vec2 q = vec2(fbm(p*.42 + vec2(0., t)), fbm(p*.42 + vec2(5.2, -t)));
      vec2 r = vec2(fbm(p*.5 + 1.2*q + vec2(1.7, 9.2) + t*.6), fbm(p*.5 + 1.2*q + vec2(8.3, 2.8) - t*.5));
      float f = fbm(p*.45 + 1.4*r + (m*.15));
      float band = 0.;
      for (int i = 0; i < 3; i++) {
        float fi = float(i);
        float y = p.y + .16*sin(p.x*1.1 + t*2.2 + fi*2.1) + .10*r.x + (fi - 1.)*.2 + .08 - uScroll*.00008;
        band += exp(-pow(y*(3.2 + fi*2.2), 2.)) * (.30 - fi*.07);
      }
      vec3 violet = vec3(.47,.27,.98), indigo = vec3(.22,.24,.82), blue = vec3(.12,.47,.98), cyan = vec3(.35,.85,1.);
      vec3 aur = mix(indigo, violet, smoothstep(-.3,.6,f));
      aur = mix(aur, blue, smoothstep(.2,.9,r.y));
      col += aur * (.03 + .09*smoothstep(-.4,.8,f));
      col += mix(violet, blue, uv.x) * band * (.5 + uPulse);
      float d = length(p - m);
      col += vec3(.45,.40,1.) * .075 * exp(-d*d*5.);
      col *= 1. - .55*smoothstep(.45, 1.25, length(p*vec2(.8,1.1)));
      col += (fract(sin(dot(uv*uRes, vec2(12.9898,78.233))) * 43758.5453) - .5) * .012;
      gl_FragColor = vec4(col, 1.);
    }`,
  depthWrite: false, depthTest: false,
});
const bgScene = new THREE.Scene();
bgScene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), aurora));
const bgCam = new THREE.Camera();

// ---------------------------------------------------------------- stars
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 2000);
const N = 1600, pos = new Float32Array(N * 3), sz = new Float32Array(N), ph = new Float32Array(N);
for (let i = 0; i < N; i++) {
  pos[i * 3] = (Math.random() - .5) * 1600; pos[i * 3 + 1] = (Math.random() - .5) * 1000; pos[i * 3 + 2] = -100 - Math.random() * 900;
  sz[i] = .6 + Math.pow(Math.random(), 6) * 3.2; ph[i] = Math.random() * 6.28;
}
const sg = new THREE.BufferGeometry();
sg.setAttribute("position", new THREE.BufferAttribute(pos, 3));
sg.setAttribute("aSize", new THREE.BufferAttribute(sz, 1));
sg.setAttribute("aPhase", new THREE.BufferAttribute(ph, 1));
const starMat = new THREE.ShaderMaterial({
  uniforms: { uTime: { value: 0 }, uPx: { value: renderer.getPixelRatio() } },
  vertexShader: `attribute float aSize; attribute float aPhase; uniform float uTime; uniform float uPx; varying float vA;
    void main(){ vec4 mv = modelViewMatrix * vec4(position,1.); vA = .55 + .45*sin(uTime*.8 + aPhase);
      gl_PointSize = aSize * uPx * (380. / -mv.z) + 1.; gl_Position = projectionMatrix * mv; }`,
  fragmentShader: `varying float vA; void main(){ float d = length(gl_PointCoord - .5); float a = smoothstep(.5, 0., d); a *= a;
    gl_FragColor = vec4(vec3(.85,.88,1.), a * vA * .85); }`,
  transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
});
const stars = new THREE.Points(sg, starMat);
scene.add(stars);

// ---------------------------------------------------------------- input (eased)
const tgt = { x: .5, y: .5 }, cur = { x: .5, y: .5 };
let scrollY = 0, pulse = 0;
window.addEventListener("pointermove", (e) => { tgt.x = e.clientX / innerWidth; tgt.y = 1 - e.clientY / innerHeight; }, { passive: true });
window.addEventListener("scroll", () => { scrollY = window.scrollY; }, { passive: true });
window.addEventListener("cosmos:warp", () => { pulse = 1; });

function resize() {
  renderer.setSize(innerWidth, innerHeight, false);
  aurora.uniforms.uRes.value.set(innerWidth, innerHeight);
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
}
addEventListener("resize", resize); resize();
renderer.autoClear = false;

const clock = new THREE.Clock();
let running = true, last = 0;
document.addEventListener("visibilitychange", () => { running = !document.hidden; if (running) requestAnimationFrame(loop); });
function loop(now) {
  if (!running) return;
  if (now - last < 21) { requestAnimationFrame(loop); return; } // ~45 fps is plenty for slow ambient motion
  last = now;
  const t = reduce ? 0 : clock.getElapsedTime();
  const k = reduce ? 1 : .045;
  cur.x += (tgt.x - cur.x) * k; cur.y += (tgt.y - cur.y) * k;
  pulse *= .965;
  aurora.uniforms.uTime.value = t; aurora.uniforms.uMouse.value.set(cur.x, cur.y);
  aurora.uniforms.uScroll.value = scrollY; aurora.uniforms.uPulse.value = pulse;
  starMat.uniforms.uTime.value = t;
  stars.rotation.y = (cur.x - .5) * .08; stars.rotation.x = -(cur.y - .5) * .05;
  stars.position.y = scrollY * .05; stars.position.z = pulse * 120;
  renderer.clear();
  renderer.render(bgScene, bgCam);
  renderer.render(scene, camera);
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
