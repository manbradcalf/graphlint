"""
graphlint playground — Interactive web UI for testing ShExC schemas.

Run with: uv run python playground.py
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from graphlint.parser import parse_shexc_to_plan
from graphlint.backends.cypher import CypherBackend
from graphlint.runner import dry_run, execute_plan

app = FastAPI()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

EXAMPLE_SHEXC = """\
PREFIX ex: <http://example.org/movies#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

ex:Movie {
    ex:title         xsd:string    ;
    ex:released      xsd:integer   ;
    ex:tagline       xsd:string ?  ;
    ex:hasActor      @ex:Person +  ;
    ex:hasDirector   @ex:Person    ;
    ex:inGenre       @ex:Genre +
}

ex:Person {
    ex:name          xsd:string    ;
    ex:born          xsd:integer ? ;
    ex:nationality   xsd:string ?
}

ex:Genre {
    ex:name          xsd:string  ;
    ex:rating        ["G" "PG" "PG-13" "R" "NC-17"] ?
}

ex:Review {
    ex:score         xsd:float     ;
    ex:summary       xsd:string    ;
    ex:reviewOf      @ex:Movie     ;
    ex:writtenBy     @ex:Person ?
}
"""


class CompileRequest(BaseModel):
    shexc: str
    strict: bool = False


class ValidateRequest(BaseModel):
    shexc: str
    bolt_uri: str
    username: str = "neo4j"
    password: str = ""
    database: str | None = None
    strict: bool = False


@app.post("/api/validate")
def validate_schema(req: ValidateRequest):
    try:
        from neo4j import GraphDatabase

        plan = parse_shexc_to_plan(req.shexc, source="<playground>", strict=req.strict)
        backend = CypherBackend()
        driver = GraphDatabase.driver(req.bolt_uri, auth=(req.username, req.password))
        report = execute_plan(
            plan, backend, driver,
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
        plan = parse_shexc_to_plan(req.shexc, source="<playground>", strict=req.strict)
        backend = CypherBackend()
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
        {"request": request, "example_shexc": EXAMPLE_SHEXC},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8420)
