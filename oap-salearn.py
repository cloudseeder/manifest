#!/usr/bin/env python3
"""SpamAssassin training HTTP service — oap-salearn.
Deploy to /usr/local/bin/oap-salearn on the mail server.

Usage:
  oap-salearn --user netgate --port 8307
  oap-salearn --user mynewscast --port 8308 --key /etc/oap-salearn-mynewscast.key
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("oap-salearn")

SA_LEARN = "/usr/local/cpanel/3rdparty/bin/sa-learn"

# Set at startup from CLI args
_api_key_path: Path = Path("/etc/oap-salearn.key")
_cpanel_user: str = ""

app = FastAPI(title="oap-salearn", version="1.0.0")


def _load_api_key() -> str:
    try:
        return _api_key_path.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(f"API key file not found: {_api_key_path}")


def _check_auth(x_api_key: str | None) -> None:
    if not x_api_key or x_api_key != _load_api_key():
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health():
    return {"status": "ok", "user": _cpanel_user}


@app.post("/train")
async def train(
    request: Request,
    label: str = Query(..., pattern="^(spam|ham)$"),
    x_api_key: str | None = Header(default=None),
):
    _check_auth(x_api_key)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty message body")

    flag = "--spam" if label == "spam" else "--ham"
    log.info("Training %s for user=%s: %d bytes", label, _cpanel_user, len(body))

    try:
        proc = await asyncio.create_subprocess_exec(
            SA_LEARN, flag, "--single",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=body)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"sa-learn not found at {SA_LEARN}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Subprocess error: {exc}")

    if proc.returncode not in (0, 6):  # 6 = already learned
        err = stderr.decode(errors="replace").strip()
        log.error("sa-learn exited %d: %s", proc.returncode, err)
        raise HTTPException(status_code=500, detail=f"sa-learn exited {proc.returncode}: {err}")

    out = stdout.decode(errors="replace").strip()
    if out:
        log.info("sa-learn: %s", out)

    return JSONResponse({"trained": True, "label": label, "bytes": len(body), "user": _cpanel_user})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OAP SpamAssassin training service")
    parser.add_argument("--user", required=True, help="cPanel username (Bayes DB owner)")
    parser.add_argument("--port", type=int, default=8307, help="Port to listen on (default: 8307)")
    parser.add_argument("--key", help="Path to API key file (default: /etc/oap-salearn-{user}.key)")
    args = parser.parse_args()

    _cpanel_user = args.user
    _api_key_path = Path(args.key) if args.key else Path(f"/etc/oap-salearn-{args.user}.key")

    log.info("Starting oap-salearn user=%s port=%d key=%s", _cpanel_user, args.port, _api_key_path)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None)
