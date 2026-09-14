"""CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from .config import Config, ConfigError
from .pipeline import run_diligence
from .render import render

app = typer.Typer(add_completion=False, help="Grounded vendor security due diligence.")
console = Console()


@app.command()
def main(
    vendor: Annotated[str, typer.Argument(help="Vendor name")],
    domain: Annotated[
        Optional[str], typer.Option(help="Vendor primary domain, e.g. datadog.com")
    ] = None,
    json_out: Annotated[
        Optional[Path], typer.Option("--json", help="Write full report JSON here")
    ] = None,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Skip the entailment gate")
    ] = False,
) -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from None

    report = run_diligence(
        vendor, vendor_domain=domain, config=config, verify=not no_verify
    )
    render(report, console)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        console.print(f"[dim]{json_out}[/dim]")


if __name__ == "__main__":
    app()
