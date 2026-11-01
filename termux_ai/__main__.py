def run() -> None:
    from .bootstrap import ensure_deps

    ensure_deps()  # downloads textual/rich/httpx on first run if missing
    from .app import main

    main()


if __name__ == "__main__":
    run()
