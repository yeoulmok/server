from fastapi import FastAPI

app = FastAPI(title="여울목 API")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "yeoulmok"}

from app.api_valleys import router as valleys_router  # noqa: E402  명세 4·5번 (/api/valleys)
app.include_router(valleys_router)

from app.api_devices import router as devices_router  # noqa: E402  명세 2·3·6·7·8번 (기기·구독)
app.include_router(devices_router)
