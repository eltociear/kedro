"""Utilities for use with click."""
from __future__ import annotations

import difflib
import logging
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import traceback
import typing
from collections import defaultdict
from importlib import import_module
from itertools import chain
from pathlib import Path
from typing import IO, Any, Iterable, Sequence

import click
import importlib_metadata
from omegaconf import OmegaConf

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
MAX_SUGGESTIONS = 3
CUTOFF = 0.5

ENV_HELP = "Kedro configuration environment name. Defaults to `local`."

ENTRY_POINT_GROUPS = {
    "global": "kedro.global_commands",
    "project": "kedro.project_commands",
    "init": "kedro.init",
    "line_magic": "kedro.line_magic",
    "hooks": "kedro.hooks",
    "cli_hooks": "kedro.cli_hooks",
    "starters": "kedro.starters",
}

logger = logging.getLogger(__name__)


def call(cmd: list[str], **kwargs: Any) -> None:  # pragma: no cover
    """Run a subprocess command and raise if it fails.

    Args:
        cmd: List of command parts.
        **kwargs: Optional keyword arguments passed to `subprocess.run`.

    Raises:
        click.exceptions.Exit: If `subprocess.run` returns non-zero code.
    """
    click.echo(" ".join(shlex.quote(c) for c in cmd))
    code = subprocess.run(cmd, **kwargs).returncode  # noqa: PLW1510, S603
    if code:
        raise click.exceptions.Exit(code=code)


def python_call(
    module: str, arguments: Iterable[str], **kwargs: Any
) -> None:  # pragma: no cover
    """Run a subprocess command that invokes a Python module."""
    call([sys.executable, "-m", module] + list(arguments), **kwargs)


def find_stylesheets() -> Iterable[str]:  # pragma: no cover
    # TODO: Deprecate this function in favour of kedro-sphinx-theme
    """Fetch all stylesheets used in the official Kedro documentation"""
    css_path = Path(__file__).resolve().parents[1] / "html" / "_static" / "css"
    return (str(css_path / "copybutton.css"),)


def forward_command(
    group: Any, name: str | None = None, forward_help: bool = False
) -> Any:
    """A command that receives the rest of the command line as 'args'."""

    def wrapit(func: Any) -> Any:
        func = click.argument("args", nargs=-1, type=click.UNPROCESSED)(func)
        func = command_with_verbosity(
            group,
            name=name,
            context_settings={
                "ignore_unknown_options": True,
                "help_option_names": [] if forward_help else ["-h", "--help"],
            },
        )(func)
        return func

    return wrapit


def _partial_match(plugin_names: str, command_name: str):
    for plugin_name in plugin_names:
        if command_name in plugin_name:
            return plugin_name
    return None


def _suggest_cli_command(
    original_command_name: str, existing_command_names: Iterable[str]
) -> str:
    matches = difflib.get_close_matches(
        original_command_name, existing_command_names, MAX_SUGGESTIONS, CUTOFF
    )

    if not matches:
        return ""

    if len(matches) == 1:
        suggestion = "\n\nDid you mean this?"
    else:
        suggestion = "\n\nDid you mean one of these?\n"
    suggestion += textwrap.indent("\n".join(matches), " " * 4)
    return suggestion


class CommandCollection(click.CommandCollection):
    """Modified from the Click one to still run the source groups function."""

    def __init__(
        self,
        *groups: tuple[str, Sequence[click.MultiCommand]],
        plugin_entry_points={},
    ):
        self.groups = [
            (title, self._merge_same_name_collections(cli_list))
            for title, cli_list in groups
        ]
        self.lazy_groups = plugin_entry_points
        sources = list(chain.from_iterable(cli_list for _, cli_list in self.groups))
        help_texts = [
            cli.help
            for cli_collection in sources
            for cli in cli_collection.sources
            if cli.help
        ]
        self._dedupe_commands(sources)
        super().__init__(
            sources=sources,  # type: ignore[arg-type]
            help="\n\n".join(help_texts),
            context_settings=CONTEXT_SETTINGS,
        )
        self.params = sources[0].params
        self.callback = sources[0].callback

    @staticmethod
    def _dedupe_commands(cli_collections: Sequence[click.CommandCollection]) -> None:
        """Deduplicate commands by keeping the ones from the last source
        in the list.
        """
        seen_names: set[str] = set()
        for cli_collection in reversed(cli_collections):
            for cmd_group in reversed(cli_collection.sources):
                cmd_group.commands = {  # type: ignore[attr-defined]
                    cmd_name: cmd
                    for cmd_name, cmd in cmd_group.commands.items()  # type: ignore[attr-defined]
                    if cmd_name not in seen_names
                }
                seen_names |= cmd_group.commands.keys()  # type: ignore[attr-defined]

        # remove empty command groups
        for cli_collection in cli_collections:
            cli_collection.sources = [
                cmd_group
                for cmd_group in cli_collection.sources
                if cmd_group.commands  # type: ignore[attr-defined]
            ]

    @staticmethod
    def _merge_same_name_collections(
        groups: Sequence[click.MultiCommand],
    ) -> list[click.CommandCollection]:
        named_groups: defaultdict[str, list[click.MultiCommand]] = defaultdict(list)
        helps: defaultdict[str, list] = defaultdict(list)
        for group in groups:
            named_groups[group.name].append(group)  # type: ignore[index]
            if group.help:
                helps[group.name].append(group.help)  # type: ignore[index]

        return [
            click.CommandCollection(
                name=group_name,
                sources=cli_list,
                help="\n\n".join(helps[group_name]),
                callback=cli_list[0].callback,
                params=cli_list[0].params,
            )
            for group_name, cli_list in named_groups.items()
            if cli_list
        ]

    def main(
        self,
        args: Any | None = None,
        prog_name: Any | None = None,
        complete_var: Any | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ):
        # Load plugins if the command is not found in the current sources
        if args is not None and args[0] not in self.list_commands(None):
            self._load_plugins(args[0])

        return super().main(
            args=args,
            prog_name=prog_name,
            complete_var=complete_var,
            standalone_mode=standalone_mode,
            **extra,
        )

    def _load_plugins(self, command_name: str) -> None:
        """Load plugins if the command is not found in the current sources."""
        ep_names = list(self.lazy_groups.keys())
        part_match = _partial_match(ep_names, command_name)
        if part_match:
            # Try to smartly load the plugin if there is partial match
            loaded_ep = _safe_load_entry_point(self.lazy_groups[part_match])
            self.add_source(loaded_ep)
            if command_name in self.list_commands(None):
                return
        # Load all plugins
        for ep in self.lazy_groups.values():
            if command_name in self.list_commands(None):
                return
            loaded_ep = _safe_load_entry_point(ep)
            self.add_source(loaded_ep)
        return

    def resolve_command(
        self, ctx: click.core.Context, args: list
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.exceptions.UsageError as exc:
            original_command_name = click.utils.make_str(args[0])
            existing_command_names = self.list_commands(ctx)
            exc.message += _suggest_cli_command(
                original_command_name, existing_command_names
            )
            raise

    def format_commands(
        self, ctx: click.core.Context, formatter: click.formatting.HelpFormatter
    ) -> None:
        for title, cli in self.groups:
            for group in cli:
                if group.sources:
                    formatter.write(
                        click.style(f"\n{title} from {group.name}", fg="green")
                    )
                    group.format_commands(ctx, formatter)


def get_pkg_version(reqs_path: (str | Path), package_name: str) -> str:
    """Get package version from requirements.txt.

    Args:
        reqs_path: Path to requirements.txt file.
        package_name: Package to search for.

    Returns:
        Package and its version as specified in requirements.txt.

    Raises:
        KedroCliError: If the file specified in ``reqs_path`` does not exist
            or ``package_name`` was not found in that file.
    """
    reqs_path = Path(reqs_path).absolute()
    if not reqs_path.is_file():
        raise KedroCliError(f"Given path '{reqs_path}' is not a regular file.")

    pattern = re.compile(package_name + r"([^\w]|$)")
    with reqs_path.open("r", encoding="utf-8") as reqs_file:
        for req_line in reqs_file:
            req_line = req_line.strip()  # noqa: PLW2901
            if pattern.search(req_line):
                return req_line

    raise KedroCliError(f"Cannot find '{package_name}' package in '{reqs_path}'.")


def _update_verbose_flag(ctx: click.Context, param: Any, value: bool) -> None:
    KedroCliError.VERBOSE_ERROR = value


def _click_verbose(func: Any) -> Any:
    """Click option for enabling verbose mode."""
    return click.option(
        "--verbose",
        "-v",
        is_flag=True,
        callback=_update_verbose_flag,
        help="See extensive logging and error stack traces.",
    )(func)


def command_with_verbosity(group: click.core.Group, *args: Any, **kwargs: Any) -> Any:
    """Custom command decorator with verbose flag added."""

    def decorator(func: Any) -> Any:
        func = _click_verbose(func)
        func = group.command(*args, **kwargs)(func)
        return func

    return decorator


class KedroCliError(click.exceptions.ClickException):
    """Exceptions generated from the Kedro CLI.

    Users should pass an appropriate message at the constructor.
    """

    VERBOSE_ERROR = False
    VERBOSE_EXISTS = True
    COOKIECUTTER_EXCEPTIONS_PREFIX = "cookiecutter.exceptions"

    def show(self, file: IO | None = None) -> None:
        if self.VERBOSE_ERROR:
            click.secho(traceback.format_exc(), nl=False, fg="yellow")
        elif self.VERBOSE_EXISTS:
            etype, value, tb = sys.exc_info()
            formatted_exception = "".join(traceback.format_exception_only(etype, value))
            cookiecutter_exception = ""
            for ex_line in traceback.format_exception(etype, value, tb):
                if self.COOKIECUTTER_EXCEPTIONS_PREFIX in ex_line:
                    cookiecutter_exception = ex_line
                    break
            click.secho(
                f"{cookiecutter_exception}{formatted_exception}Run with --verbose to see the full exception",
                fg="yellow",
            )
        else:
            etype, value, _ = sys.exc_info()
            formatted_exception = "".join(traceback.format_exception_only(etype, value))
            click.secho(
                f"{formatted_exception}",
                fg="yellow",
            )


def _clean_pycache(path: Path) -> None:
    """Recursively clean all __pycache__ folders from `path`.

    Args:
        path: Existing local directory to clean __pycache__ folders from.
    """
    to_delete = [each.resolve() for each in path.rglob("__pycache__")]

    for each in to_delete:
        shutil.rmtree(each, ignore_errors=True)


def split_string(ctx: click.Context, param: Any, value: str) -> list[str]:
    """Split string by comma."""
    return [item.strip() for item in value.split(",") if item.strip()]


def split_node_names(ctx: click.Context, param: Any, to_split: str) -> list[str]:
    """Split string by comma, ignoring commas enclosed by square parentheses.
    This avoids splitting the string of nodes names on commas included in
    default node names, which have the pattern
    <function_name>([<input_name>,...]) -> [<output_name>,...])

    Note:
        - `to_split` will have such commas if and only if it includes a
        default node name. User-defined node names cannot include commas
        or square brackets.
        - This function will no longer be necessary from Kedro 0.19.*,
        in which default node names will no longer contain commas

    Args:
        to_split: the string to split safely

    Returns:
        A list containing the result of safe-splitting the string.
    """
    result = []
    argument, match_state = "", 0
    for char in to_split + ",":
        if char == "[":
            match_state += 1
        elif char == "]":
            match_state -= 1
        if char == "," and match_state == 0 and argument:
            argument = argument.strip()
            result.append(argument)
            argument = ""
        else:
            argument += char
    return result


def env_option(func_: Any | None = None, **kwargs: Any) -> Any:
    """Add `--env` CLI option to a function."""
    default_args = {"type": str, "default": None, "help": ENV_HELP}
    kwargs = {**default_args, **kwargs}
    opt = click.option("--env", "-e", **kwargs)
    return opt(func_) if func_ else opt


def _check_module_importable(module_name: str) -> None:
    try:
        import_module(module_name)
    except ImportError as exc:
        raise KedroCliError(
            f"Module '{module_name}' not found. Make sure to install required project "
            f"dependencies by running the 'pip install -r requirements.txt' command first."
        ) from exc


def _get_entry_points(name: str) -> Any:
    """Get all kedro related entry points"""
    return importlib_metadata.entry_points().select(  # type: ignore[no-untyped-call]
        group=ENTRY_POINT_GROUPS[name]
    )


def _safe_load_entry_point(
    entry_point: Any,
) -> Any:
    """Load entrypoint safely, if fails it will just skip the entrypoint."""
    try:
        return entry_point.load()
    except Exception as exc:
        logger.warning(
            "Failed to load %s commands from %s. Full exception: %s",
            entry_point.module,
            entry_point,
            exc,
        )
        return


def load_entry_points(name: str) -> Sequence[click.MultiCommand]:
    """Load package entry point commands.

    Args:
        name: The key value specified in ENTRY_POINT_GROUPS.

    Raises:
        KedroCliError: If loading an entry point failed.

    Returns:
        List of entry point commands.

    """

    entry_point_commands = []
    for entry_point in _get_entry_points(name):
        loaded_entry_point = _safe_load_entry_point(entry_point)
        if loaded_entry_point:
            entry_point_commands.append(loaded_entry_point)
    return entry_point_commands


@typing.no_type_check
def _config_file_callback(ctx: click.Context, param: Any, value: Any) -> Any:
    """CLI callback that replaces command line options
    with values specified in a config file. If command line
    options are passed, they override config file values.
    """

    ctx.default_map = ctx.default_map or {}
    section = ctx.info_name

    if value:
        config = OmegaConf.to_container(OmegaConf.load(value))[section]
        for key, value in config.items():
            _validate_config_file(key)
        ctx.default_map.update(config)

    return value


def _validate_config_file(key: str) -> None:
    """Validate the keys provided in the config file against the accepted keys."""
    from kedro.framework.cli.project import run

    run_args = [click_arg.name for click_arg in run.params]
    run_args.remove("config")
    if key not in run_args:
        KedroCliError.VERBOSE_EXISTS = False
        message = _suggest_cli_command(key, run_args)  # type: ignore[arg-type]
        raise KedroCliError(
            f"Key `{key}` in provided configuration is not valid. {message}"
        )


def _split_params(ctx: click.Context, param: Any, value: Any) -> Any:
    if isinstance(value, dict):
        return value
    dot_list = []
    for item in split_string(ctx, param, value):
        equals_idx = item.find("=")
        if equals_idx == -1:
            # If an equals sign is not found, fail with an error message.
            ctx.fail(
                f"Invalid format of `{param.name}` option: "
                f"Item `{item}` must contain a key and a value separated by `=`."
            )
        # Split the item into key and value
        key, _, val = item.partition("=")
        key = key.strip()
        if not key:
            # If the key is empty after stripping whitespace, fail with an error message.
            ctx.fail(
                f"Invalid format of `{param.name}` option: Parameter key "
                f"cannot be an empty string."
            )
        # Add "key=value" pair to dot_list.
        dot_list.append(f"{key}={val}")

    conf = OmegaConf.from_dotlist(dot_list)
    return OmegaConf.to_container(conf)


def _split_load_versions(ctx: click.Context, param: Any, value: str) -> dict[str, str]:
    """Split and format the string coming from the --load-versions
    flag in kedro run, e.g.:
    "dataset1:time1,dataset2:time2" -> {"dataset1": "time1", "dataset2": "time2"}

    Args:
        value: the string with the contents of the --load-versions flag.

    Returns:
        A dictionary with the formatted load versions data.
    """
    if not value:
        return {}

    lv_tuple = tuple(chain.from_iterable(value.split(",") for value in [value]))

    load_versions_dict = {}
    for load_version in lv_tuple:
        load_version = load_version.strip()  # noqa: PLW2901
        load_version_list = load_version.split(":", 1)
        if len(load_version_list) != 2:  # noqa: PLR2004
            raise KedroCliError(
                f"Expected the form of 'load_versions' to be "
                f"'dataset_name:YYYY-MM-DDThh.mm.ss.sssZ',"
                f"found {load_version} instead"
            )
        load_versions_dict[load_version_list[0]] = load_version_list[1]

    return load_versions_dict
