"""Run the installed incident console with local, single-worker defaults."""

import argparse

from app.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    import uvicorn

    settings = Settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, workers=1)


if __name__ == "__main__":
    main()
