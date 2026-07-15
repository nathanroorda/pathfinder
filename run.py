from log import configure_logging
configure_logging()

import uvicorn

from app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)