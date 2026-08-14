// Minimal static file server for the built site (public/).
// Uses serve-handler, already a project dependency; non-root friendly.
import handler from "serve-handler"
import http from "http"

const PORT = Number(process.env.PORT || 8080)
const PUBLIC_DIR = process.env.PUBLIC_DIR || "public"

const server = http.createServer((request, response) => {
  return handler(request, response, {
    public: PUBLIC_DIR,
    rewrites: [{ source: "**", destination: "/404.html" }],
    cleanUrls: true,
  })
})

server.listen(PORT, "0.0.0.0", () => {
  console.log("nLab-Chinese static server listening on port " + PORT)
})
