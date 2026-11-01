from fastapi import FastAPI

app = FastAPI(title="여울목 API")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "yeoulmok"}
