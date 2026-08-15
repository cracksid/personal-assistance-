/**
 * The animated background, drawn by a WebGL fragment shader.
 *
 * WHAT A SHADER IS.
 *
 * A fragment shader is a small program that runs ONCE PER PIXEL, on the
 * graphics card, every frame. It is handed the pixel's coordinates and has
 * to answer one question: what colour is this pixel? There are no loops
 * over the screen and no drawing commands -- every pixel is computed
 * independently and in parallel, which is why a GPU can do millions of them
 * at 60fps while the same work in JavaScript would crawl.
 *
 * The language is GLSL, which looks like C. The string below IS the
 * program; it is compiled at runtime by the browser and uploaded to the
 * card.
 *
 * WHY THIS DRAWS TWO TRIANGLES.
 *
 * WebGL only knows how to draw triangles, so to run a shader over the whole
 * screen you give it two that cover it exactly. The triangles are a
 * formality -- all the work happens per-pixel in the fragment shader.
 *
 * THE DESIGN CONSTRAINT.
 *
 * This is behind a chat window, so it must never fight the text. Everything
 * here is deliberately low-contrast and slow: a drifting grid, a soft pulse
 * from the centre, and a vignette that darkens the edges where the message
 * bubbles sit. If you can read it comfortably, the shader is doing its job.
 */

import { useEffect, useRef } from "react";

const VERTEX_SHADER = `
  attribute vec2 position;
  void main() {
    gl_Position = vec4(position, 0.0, 1.0);
  }
`;

const FRAGMENT_SHADER = `
  precision mediump float;

  uniform vec2  resolution;
  uniform float time;

  // Cheap pseudo-random from a 2D coordinate. The magic numbers are
  // arbitrary irrational-ish values -- any large non-repeating pair works.
  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }

  // Distance to the nearest line of a grid of the given size, in [0,1].
  // fract() gives position within a cell; the two-sided min makes the
  // measurement symmetric so lines are not twice as thick on one side.
  float gridLine(vec2 uv, float size) {
    vec2 cell = fract(uv * size);
    vec2 edge = min(cell, 1.0 - cell);
    return min(edge.x, edge.y);
  }

  void main() {
    // Normalised coordinates, corrected for aspect so circles are round.
    vec2 uv = gl_FragCoord.xy / resolution.xy;
    vec2 centred = uv - 0.5;
    centred.x *= resolution.x / resolution.y;

    float dist = length(centred);

    // --- base -------------------------------------------------------
    // Very dark blue, slightly lighter towards the middle.
    vec3 colour = mix(vec3(0.020, 0.043, 0.075), vec3(0.008, 0.016, 0.031), dist * 1.4);

    // --- drifting grid ----------------------------------------------
    // Two grids at different scales moving at different speeds reads as
    // depth without any actual 3D.
    vec2 slow = uv + vec2(time * 0.006, time * -0.004);
    vec2 fast = uv + vec2(time * -0.011, time * 0.008);

    float coarse = 1.0 - smoothstep(0.0, 0.006, gridLine(slow, 14.0));
    float fine   = 1.0 - smoothstep(0.0, 0.010, gridLine(fast, 42.0));

    colour += vec3(0.10, 0.45, 0.65) * coarse * 0.16;
    colour += vec3(0.08, 0.35, 0.55) * fine   * 0.05;

    // --- reactor glow -----------------------------------------------
    // A soft pulse from the centre, breathing slowly. pow() concentrates
    // it so the falloff is tight rather than a flat wash.
    float breathe = 0.5 + 0.5 * sin(time * 0.6);
    float glow = pow(max(0.0, 1.0 - dist * 1.6), 3.0);
    colour += vec3(0.05, 0.55, 0.85) * glow * (0.10 + breathe * 0.05);

    // --- sweeping scan ----------------------------------------------
    // A faint horizontal band travelling down the screen, like a CRT or a
    // radar refresh. mod() wraps it around forever.
    float scanY = mod(time * 0.07, 1.4) - 0.2;
    float scan = exp(-pow((uv.y - scanY) * 26.0, 2.0));
    colour += vec3(0.10, 0.55, 0.75) * scan * 0.14;

    // --- scanlines and grain ----------------------------------------
    float lines = sin(gl_FragCoord.y * 1.7) * 0.5 + 0.5;
    colour -= lines * 0.010;
    colour += (hash(gl_FragCoord.xy + time) - 0.5) * 0.014;

    // --- vignette ---------------------------------------------------
    // Darkens the edges, which is where the text sits. Readability first.
    colour *= 1.0 - smoothstep(0.35, 1.15, dist) * 0.55;

    gl_FragColor = vec4(colour, 1.0);
  }
`;

function compile(gl: WebGLRenderingContext, type: number, source: string) {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.warn("Shader failed to compile:", gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

export function ShaderBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // Someone who has asked their system to reduce motion should not be
    // given a permanently animating background. The CSS gradient behind
    // the canvas is the fallback, so doing nothing here is a valid result.
    const stillPlease = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (stillPlease) return;

    const gl = canvas.getContext("webgl", { antialias: false, alpha: false });
    if (!gl) return; // No WebGL: the CSS gradient shows through instead.

    const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    if (!vertex || !fragment) return;

    const program = gl.createProgram();
    if (!program) return;
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.warn("Shader program failed to link:", gl.getProgramInfoLog(program));
      return;
    }
    gl.useProgram(program);

    // Two triangles covering the whole clip-space square, as -1..1 corners.
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW,
    );

    const position = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

    const resolutionAt = gl.getUniformLocation(program, "resolution");
    const timeAt = gl.getUniformLocation(program, "time");

    function resize() {
      if (!canvas || !gl) return;
      // Capped at 1.5x rather than the full device pixel ratio: this is a
      // soft background, and a 4K display would otherwise shade four times
      // as many pixels every frame for no visible gain.
      const scale = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = Math.floor(window.innerWidth * scale);
      canvas.height = Math.floor(window.innerHeight * scale);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(resolutionAt, canvas.width, canvas.height);
    }

    resize();
    window.addEventListener("resize", resize);

    let frame = 0;
    const started = performance.now();

    function draw() {
      if (!gl) return;
      // document.hidden: a background tab still gets rAF in some browsers,
      // and shading a screen nobody is looking at is pure battery drain.
      if (!document.hidden) {
        gl.uniform1f(timeAt, (performance.now() - started) / 1000);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }
      frame = requestAnimationFrame(draw);
    }
    draw();

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      gl.deleteProgram(program);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      gl.deleteBuffer(buffer);
    };
  }, []);

  return <canvas ref={canvasRef} className="shader-bg" aria-hidden="true" />;
}
