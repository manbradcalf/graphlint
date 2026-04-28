"""
graphlint playground — Interactive web UI for testing SHACL schemas.

Run with: uv run python playground.py
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from graphlint.parser import parse_schema
from graphlint.backends.cypher import CypherBackend
from graphlint.runner import dry_run, execute_plan

app = FastAPI()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

EXAMPLE_SHACL = (Path(__file__).parent / "examples" / "movies.shacl.ttl").read_text()


class CompileRequest(BaseModel):
    schema: str = ""
    strict: bool = False
    database_type: str = "neo4j"


class ValidateRequest(BaseModel):
    schema: str = ""
    bolt_uri: str
    username: str = "neo4j"
    password: str = ""
    database: str | None = None
    strict: bool = False
    database_type: str = "neo4j"


@app.post("/api/validate")
def validate_schema(req: ValidateRequest):
    try:
        from neo4j import GraphDatabase

        plan = parse_schema(
            req.schema, source="<playground>", strict=req.strict
        )
        backend = CypherBackend(dialect=req.database_type)
        driver = GraphDatabase.driver(req.bolt_uri, auth=(req.username, req.password))
        report = execute_plan(
            plan,
            backend,
            driver,
            database=req.database or None,
            target_uri=req.bolt_uri,
        )
        driver.close()
        return {"ok": True, "report": report.to_dict()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/compile")
def compile_schema(req: CompileRequest):
    try:
        plan = parse_schema(
            req.schema, source="<playground>", strict=req.strict
        )
        backend = CypherBackend(dialect=req.database_type)
        cypher = dry_run(plan, backend)
        return {
            "ok": True,
            "plan": plan.to_dict(),
            "cypher": cypher,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "playground.html",
        {
            "request": request,
            "example_shacl": EXAMPLE_SHACL,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
