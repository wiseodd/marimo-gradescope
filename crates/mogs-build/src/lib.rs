//! `mogs build` and `mogs grade`.
//!
//! [`build`] writes the student notebook and the Gradescope autograder zip, after grading the
//! solved and the blank student copy in an environment with Gradescope's requirements
//! ([`environment`]). [`regrade`] grades one submission the same way, and [`report`] turns
//! both into the text the commands print.
//!
//! Python reads the source notebook and computes what follows from it: the student notebook,
//! the bundle, the requirements and the warnings. That, text with Python's rules and Python's
//! exceptions come from the caller through [`Interpreter`]. Text that may hold a path is an
//! [`OsString`], since a path may hold bytes that are not UTF-8. Linux and macOS only, but for
//! [`DIST`], [`PARTS`] and [`remove_output`], as a failed build removes its output on every
//! platform.

/// The output folder next to the source notebook.
pub const DIST: &str = "dist";
/// The parts of `DIST` a build replaces.
pub const PARTS: [&str; 2] = ["student", "autograder"];

/// Remove what a failed build of `folder` leaves, ignoring errors, as `shutil.rmtree(...,
/// ignore_errors=True)`.
pub fn remove_output(folder: &std::path::Path) {
    for part in PARTS {
        let _ = std::fs::remove_dir_all(folder.join(DIST).join(part));
    }
}

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;
