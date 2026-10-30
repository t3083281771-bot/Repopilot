from pydantic import BaseModel, Field


class Chunk(BaseModel):
    id: str
    repo_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    content: str
    kind: str = "code"
    embedding: list[float] = Field(default_factory=list)
    score: float = 0.0
