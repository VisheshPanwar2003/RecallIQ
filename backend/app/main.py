from fastapi import FastAPI

from app.documents import router as documents_router


app = FastAPI(title="RecallIQ API")
app.include_router(documents_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to RecallIQ API"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "recalliq-backend"}
