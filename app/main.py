from fastapi import FastAPI

app = FastAPI(title="여울목 API")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "yeoulmok"}

from app.api_valleys import router as valleys_router  # noqa: E402  명세 4·5번 (/api/valleys)
app.include_router(valleys_router)
