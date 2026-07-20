import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "channelforge.app:app",
        host=os.environ.get("CF_HOST", "0.0.0.0"),  # nosec B104
        port=int(os.environ.get("CF_PORT", "5100")),
        log_level="info",
    )
