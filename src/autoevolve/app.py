from collections.abc import Sequence
from importlib import import_module

import click
import typer
from typer.core import TyperGroup
from typer.main import get_command


class AutoevolveGroup(TyperGroup):
    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        command_names = self.list_commands(ctx)
        sections: dict[str, list[tuple[str, str]]] = {
            title: [] for title in ("Human", "Lifecycle", "Inspect", "Analytics")
        }
        command_width = max((len(name) for name in command_names), default=0)

        for command_name in command_names:
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            section = getattr(command, "rich_help_panel", None) or "Other"
            sections.setdefault(section, []).append(
                (
                    command_name.ljust(command_width),
                    command.get_short_help_str(formatter.width),
                )
            )

        for title, rows in sections.items():
            if not rows:
                continue
            with formatter.section(title):
                formatter.write_dl(rows)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if self.epilog is None:
            return
        formatter.write_paragraph()
        formatter.write(f"{self.epilog}\n")


app = typer.Typer(
    cls=AutoevolveGroup,
    help="Git-backed experiment loops for coding agents.",
    epilog="\n".join(
        [
            "Examples:",
            '  autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844',
            "  autoevolve record",
            "  autoevolve log",
            "  autoevolve recent --limit 5",
            "  autoevolve best --max benchmark_score --limit 5",
            "",
            'Run "autoevolve <command> --help" for command-specific details.',
        ]
    ),
    invoke_without_command=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.callback()
def main_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None and not ctx.resilient_parsing:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def main(argv: Sequence[str] | None = None) -> int:
    command = get_command(app)
    try:
        command.main(
            args=list(argv) if argv is not None else None,
            prog_name="autoevolve",
            standalone_mode=False,
        )
        return 0
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except typer.Abort:
        typer.echo("Aborted!", err=True)
        return 1
    except typer.Exit as error:
        return error.exit_code
    except Exception as error:
        typer.echo(str(error), err=True)
        return 1


for module in ("human", "lifecycle", "inspect", "analytics"):
    import_module(f"autoevolve.commands.{module}")


if __name__ == "__main__":
    raise SystemExit(main())
