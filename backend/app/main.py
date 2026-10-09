from fastapi import FastAPI


app = FastAPI(title="RecallIQ API")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to RecallIQ API"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "recalliq-backend"}
