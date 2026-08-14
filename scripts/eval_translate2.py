#!/usr/bin/env python3
"""Compare translation quality across GGUF models using the real pipeline prompt.

Usage:
    python3 scripts/eval_translate2.py <model.gguf> <port> <label> [no_reason]
If 'no_reason' is passed, llama-server is started with --reasoning-budget 0 to
suppress chain-of-thought and force direct translation output.
"""
import json, subprocess, sys, time, urllib.request

MODEL = sys.argv[1]; PORT = int(sys.argv[2]); LABEL = sys.argv[3]
NO_REASON = len(sys.argv) > 4 and sys.argv[4] == "no_reason"
PROMPT = open("scripts/eval_prompt.txt", encoding="utf-8").read()

SYSTEM = (
    "You are a strict academic translation engine. Translate the English text "
    "inside <source_text> into rigorous mathematical Chinese.\n"
    "1. DO NOT translate proper names of persons (Grothendieck, Kan, Yoneda, etc.); "
    "keep them in original English.\n"
    "2. Preserve all Markdown, LaTeX ($...$, $$...$$) and wiki-links [[...]] exactly.\n"
    "3. If a concept cannot be translated precisely, keep the English term."
)

def http_json(url, method="GET", payload=None, timeout=400):
    req = urllib.request.Request(url, method=method); data = None
    if payload is not None:
        data = json.dumps(payload).encode(); req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def main():
    cmd = ["llama-server","-m",MODEL,"--host","127.0.0.1","--port",str(PORT),
           "-c","4096","--temp","0.0","--top-p","0.1","--no-webui","--log-disable"]
    if NO_REASON:
        cmd += ["--reasoning-budget","0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try: http_json(f"http://127.0.0.1:{PORT}/health", timeout=5); break
            except Exception: time.sleep(1)
        user = "<source_text>\n" + PROMPT + "\n</source_text>"
        payload = {"model": LABEL, "messages": [
            {"role":"system","content":SYSTEM},
            {"role":"user","content": user}],
            "temperature":0.0, "top_p":0.1, "max_tokens":2048}
        t0 = time.time()
        res = http_json(f"http://127.0.0.1:{PORT}/v1/chat/completions","POST",payload)
        dt = time.time()-t0
        msg = res["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        fr = res["choices"][0].get("finish_reason")
        print(f"\n================ {LABEL} ================")
        print(f"elapsed={dt:.1f}s finish_reason={fr} completion_tokens={res.get('usage',{}).get('completion_tokens')}")
        if reasoning: print(f"--- reasoning_content ({len(reasoning)} ch) ---\n{reasoning[:400]}")
        print(f"--- content ---\n{content}\n--- end ---")
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill()

if __name__ == "__main__": main()
