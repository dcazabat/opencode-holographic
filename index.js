// OpenCode V2 loads a local plugin from a DIRECTORY and falls back to `<dir>/index`.
// The TypeScript source lives in index.ts + src/, compiled to dist/ by `npm run build`.
export { default } from "./dist/index.js";
