"""FastAPI application exposing the offline deadlock analyzer."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.solver import ValidationError, analyze_protocol

app = FastAPI(
    title="Two-Party Message Protocol Deadlock Analyzer",
    version="1.0.0",
    description=(
        "Explores every reachable configuration of a two-party send/receive "
        "protocol with one one-slot queue per direction."
    ),
)


class PartySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    states: list[str] = Field(description="1 to 8 distinct state names")
    initial: str = Field(description="Initial state, must be in states")
    finals: list[str] = Field(description="Non-empty set of final states")


class TransitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(description="Globally unique transition id")
    type: Literal["send", "receive"]
    from_: str = Field(alias="from", description="Source state, determines owner")
    to: str = Field(description="Target state of the owner party")
    message: str


class ProtocolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    A: PartySpec
    B: PartySpec
    transitions: list[TransitionSpec] = Field(
        description="At most 30 globally unique transitions"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deadlocks")
def analyze_deadlocks(protocol: ProtocolSpec) -> dict:
    payload = protocol.model_dump(by_alias=True)
    try:
        return analyze_protocol(payload)
    except ValidationError as exc:
        # Semantic problems: illegal references, duplicate ids, counts out of
        # range, too many message kinds, etc.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
