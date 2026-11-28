//! The `mogs` command line, with the names and help texts of v0.7.

use std::ffi::OsString;
use std::path::PathBuf;

use clap::Parser;

/// The `mogs` command.
#[derive(Debug, Parser)]
#[command(
    name = "mogs",
    version,
    about = "Build Gradescope autograders from marimo notebooks.",
    subcommand_required = true,
    arg_required_else_help = false,
    disable_help_subcommand = true,
    disable_version_flag = true
)]
pub struct Cli {
    /// The command to run.
    #[command(subcommand)]
    pub command: Command,
    /// `--version`, without the `-V` of clap. clap prints the version and exits on it, so this
    /// holds nothing.
    #[arg(long, action = clap::ArgAction::Version, help = "Print version")]
    pub version: (),
}

/// A `mogs` command and its arguments.
#[derive(Debug, PartialEq, Eq, clap::Subcommand)]
pub enum Command {
    #[command(
        about = "start a new assignment",
        long_about = "Create DIR with one notebook, DIR/<name of DIR>.py, holding the assignment \
                      cell, a guide and two example questions."
    )]
    Init {
        #[arg(value_name = "DIR", help = "new assignment folder")]
        dir: PathBuf,
    },
    #[command(
        about = "open the source notebook in marimo's editor",
        long_about = "Run `marimo edit --sandbox` on the source notebook PATH, or the one in \
                      the assignment folder PATH, with the marimo installed next to mogs."
    )]
    Edit {
        #[arg(
            value_name = "PATH",
            help = "source notebook, or the assignment folder with it"
        )]
        path: PathBuf,
    },
    #[command(
        about = "build the student notebook and the autograder zip",
        long_about = "Build DIR/dist/student/ and DIR/dist/autograder/ (the autograder zip and \
                      the solved student notebook) from the notebook in DIR, then grade the \
                      solved and the blank student copy."
    )]
    Build {
        #[arg(
            value_name = "DIR",
            help = "assignment folder with the source notebook"
        )]
        dir: PathBuf,
    },
    /// Gradescope's entry point, which `run_autograder` runs: grade the submission and write
    /// `results.json`, whatever happens. Not in `--help`.
    #[command(
        hide = true,
        about = "Gradescope entry point: grade /autograder/submission and write results.json."
    )]
    Autograde {
        #[arg(long, default_value = "/autograder/source")]
        source: PathBuf,
        #[arg(long, default_value = "/autograder/submission")]
        submission: PathBuf,
        #[arg(long, default_value = "/autograder/results/results.json")]
        results: PathBuf,
        #[arg(long, default_value = "/tmp/mogs")]
        workdir: PathBuf,
    },
}

/// What to show instead of running a command: help, the version or a usage error.
#[derive(Debug, PartialEq, Eq)]
pub struct Exit {
    /// The text, which ends with a newline.
    pub text: String,
    /// Whether the text goes to stderr: usage errors do, help and the version do not.
    pub to_stderr: bool,
    /// The exit code: 0 for help and the version, 2 for usage errors.
    pub code: i32,
}

/// The command in `args`, the arguments after the program name.
///
/// # Errors
///
/// The [`Exit`] for `--help`, `--version` or a usage error.
pub fn parse(args: impl IntoIterator<Item = OsString>) -> Result<Command, Exit> {
    let argv = std::iter::once(OsString::from("mogs")).chain(args);

    Cli::try_parse_from(argv)
        .map(|cli| cli.command)
        .map_err(|error| Exit {
            text: error.render().to_string(),
            to_stderr: error.use_stderr(),
            code: error.exit_code(),
        })
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;

    use super::*;

    fn parsed(args: &[&str]) -> Result<Command, Exit> {
        parse(args.iter().map(OsString::from))
    }

    #[test]
    fn command_is_valid() {
        Cli::command().debug_assert();
    }

    /// Each command's name, help and description, then `NAME: help` for each argument.
    fn shown(command: &clap::Command) -> Vec<String> {
        let text = |text: Option<&clap::builder::StyledStr>| {
            text.map(ToString::to_string).unwrap_or_default()
        };
        let arguments = command.get_positionals().map(|arg| {
            let names = arg.get_value_names().unwrap_or_default().join(" ");
            format!("{names}: {}", text(arg.get_help()))
        });

        [
            command.get_name().to_owned(),
            text(command.get_about()),
            text(command.get_long_about()),
        ]
        .into_iter()
        .chain(arguments)
        .collect()
    }

    #[test]
    fn help_lists_the_same_commands() {
        let expected = [
            vec![
                "init",
                "start a new assignment",
                "Create DIR with one notebook, DIR/<name of DIR>.py, holding the assignment \
                 cell, a guide and two example questions.",
                "DIR: new assignment folder",
            ],
            vec![
                "edit",
                "open the source notebook in marimo's editor",
                "Run `marimo edit --sandbox` on the source notebook PATH, or the one in the \
                 assignment folder PATH, with the marimo installed next to mogs.",
                "PATH: source notebook, or the assignment folder with it",
            ],
            vec![
                "build",
                "build the student notebook and the autograder zip",
                "Build DIR/dist/student/ and DIR/dist/autograder/ (the autograder zip and the \
                 solved student notebook) from the notebook in DIR, then grade the solved and \
                 the blank student copy.",
                "DIR: assignment folder with the source notebook",
            ],
        ];

        let cli = Cli::command();
        let commands: Vec<Vec<String>> = cli
            .get_subcommands()
            .filter(|command| !command.is_hide_set())
            .map(shown)
            .collect();

        assert_eq!(commands, expected);
        assert_eq!(
            cli.get_about().map(ToString::to_string).as_deref(),
            Some("Build Gradescope autograders from marimo notebooks.")
        );
    }

    #[test]
    fn parses_each_command() {
        assert_eq!(
            parsed(&["init", "hw"]),
            Ok(Command::Init { dir: "hw".into() })
        );
        assert_eq!(
            parsed(&["edit", "hw/hw.py"]),
            Ok(Command::Edit {
                path: "hw/hw.py".into()
            })
        );
        assert_eq!(
            parsed(&["build", "hw"]),
            Ok(Command::Build { dir: "hw".into() })
        );
    }

    #[test]
    fn grade_is_an_unknown_subcommand() {
        let exit = parsed(&["grade", "hw", "sub.zip"]).unwrap_err();

        assert!(exit.to_stderr);
        assert_eq!(exit.code, 2);
        assert!(
            exit.text
                .starts_with("error: unrecognized subcommand 'grade'"),
            "{}",
            exit.text
        );
    }

    #[test]
    fn autograde_defaults_to_gradescope_paths() {
        assert_eq!(
            parsed(&["autograde"]),
            Ok(Command::Autograde {
                source: "/autograder/source".into(),
                submission: "/autograder/submission".into(),
                results: "/autograder/results/results.json".into(),
                workdir: "/tmp/mogs".into(),
            })
        );
        assert_eq!(
            parsed(&[
                "autograde",
                "--source",
                "s",
                "--submission",
                "u",
                "--results",
                "r.json",
                "--workdir",
                "w",
            ]),
            Ok(Command::Autograde {
                source: "s".into(),
                submission: "u".into(),
                results: "r.json".into(),
                workdir: "w".into(),
            })
        );
    }

    #[test]
    fn autograde_is_hidden() {
        let cli = Cli::command();
        let hidden: Vec<&str> = cli
            .get_subcommands()
            .filter(|command| command.is_hide_set())
            .map(clap::Command::get_name)
            .collect();

        assert_eq!(hidden, ["autograde"]);
    }

    #[test]
    fn version_prints_the_package_version() {
        assert_eq!(
            parsed(&["--version"]),
            Err(Exit {
                text: format!("mogs {}\n", env!("CARGO_PKG_VERSION")),
                to_stderr: false,
                code: 0,
            })
        );
    }

    #[test]
    fn help_goes_to_stdout_and_exits_0() {
        let exit = parsed(&["init", "--help"]).unwrap_err();

        assert!(!exit.to_stderr);
        assert_eq!(exit.code, 0);
        assert!(
            exit.text.contains("Usage: mogs init <DIR>"),
            "{}",
            exit.text
        );
    }

    #[test]
    fn usage_errors_go_to_stderr_and_exit_2() {
        for args in [
            &[][..],
            &["nope"],
            &["init"],
            &["build"],
            &["init", "a", "b"],
            &["-V"],
        ] {
            let exit = parsed(args).unwrap_err();

            assert!(exit.to_stderr, "{args:?}");
            assert_eq!(exit.code, 2, "{args:?}");
            assert!(exit.text.starts_with("error: "), "{}", exit.text);
        }
    }
}
