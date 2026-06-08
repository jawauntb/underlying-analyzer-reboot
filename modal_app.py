from __future__ import annotations

from collections.abc import Callable

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "Flask>=3.0,<4",
        "flask-cors>=5.0,<6",
        "matplotlib>=3.9,<4",
        "numpy>=2.0,<3",
        "pandas>=2.2,<3",
        "requests>=2.32,<3",
        "yfinance>=1.4.1,<2",
    )
    .add_local_dir("app", remote_path="/root/app", copy=True)
)

app = modal.App("jawaun-underlying-terminal", image=image)
secrets = [modal.Secret.from_name("underlying-analyzer-env")]


@app.function(cpu=1.0, memory=2048, timeout=300, scaledown_window=300, secrets=secrets)
@modal.concurrent(max_inputs=10)
@modal.wsgi_app(label="jawaun-underlying-terminal")
def flask_app() -> Callable:
    import sys

    sys.path.insert(0, "/root")

    from app.main import create_app

    return create_app()
