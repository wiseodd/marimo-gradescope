//! The PDF of the written answers, for questions graded by hand.
//!
//! Python reads the notebook and builds the questions: per question a heading, then the
//! prompt and answer cells in notebook order. This crate renders them with Typst in-process.
//! A page break separates questions. A Markdown part shows with LaTeX math, through the
//! vendored Typst packages cmarker and mitex; any other part shows as plain text.

mod document;
pub mod formulas;
mod world;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::Duration;

use serde::Serialize;

use document::{Failure, Typst};
pub use document::{PLAIN_NOTE, SOURCE_NOTE};

/// The status message shown while the PDF is made.
pub const STATUS: &str = "Creating the PDF of your written answers…";

/// How often `render` asks the host whether to stop.
const POLL: Duration = Duration::from_millis(50);

/// The stack of the rendering thread, as large as a main thread's on Linux.
const STACK: usize = 8 * 1024 * 1024;

/// One question's page of the PDF.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Question {
    /// The heading.
    pub title: String,
    /// The prompt and answer cells in notebook order, or a note that there is no answer.
    pub parts: Vec<Part>,
}

/// One prompt or answer cell, as it goes into the PDF.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Part {
    /// Markdown, or plain text for `Style::Plain` and `Style::Note`.
    pub text: String,
    /// Whether it is a prompt or an answer.
    pub role: Role,
    /// How `text` is rendered.
    pub style: Style,
    /// A small note under the part, which the fallbacks set.
    note: Option<&'static str>,
}

impl Part {
    /// A part without a note.
    pub fn new(text: impl Into<String>, role: Role, style: Style) -> Self {
        Self {
            text: text.into(),
            role,
            style,
            note: None,
        }
    }
}

/// What a part of the PDF is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Prompt,
    Answer,
}

/// How a part of the PDF is rendered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Style {
    Markdown,
    Plain,
    Note,
}

/// Why `render` gave no PDF.
#[derive(Debug, PartialEq, Eq)]
pub enum RenderError {
    /// Typst failed even on the plain-text document, with its error messages.
    Typst(String),
    /// The host asked to stop.
    Interrupted,
    /// The rendering thread panicked, with the panic's message.
    Panicked(String),
}

/// What `render` needs from its caller.
pub trait Signals {
    /// Whether to stop now, asked about every `POLL` while Typst works.
    fn interrupted(&self) -> bool;
}

/// The PDF of `questions`; parts that break the document are rendered with less.
///
/// Typst runs on its own thread, so the caller can stop waiting at once when `host` says so;
/// the thread then ends after the Typst run it is in.
pub fn render(questions: Vec<Question>, host: &dyn Signals) -> Result<Vec<u8>, RenderError> {
    stoppable(host, move |stop| {
        let pdf = Typst { stop }.pdf(&questions);

        // Typst keeps its caches between runs; drop what recent runs did not use.
        typst::comemo::evict(10);

        pdf
    })
}

/// Run `work` on a new thread until it ends or `host` says to stop.
fn stoppable<T: Send + 'static>(
    host: &dyn Signals,
    work: impl FnOnce(&AtomicBool) -> Result<T, Failure> + Send + 'static,
) -> Result<T, RenderError> {
    let stop = Arc::new(AtomicBool::new(false));
    let (send, receive) = mpsc::channel();
    let stop_for_work = Arc::clone(&stop);

    let worker = thread::Builder::new()
        .name("mogs-pdf".into())
        .stack_size(STACK)
        .spawn(move || {
            // The receiver is gone only after a stop, when nobody wants the result.
            let _ = send.send(work(&stop_for_work));
        })
        .expect("the PDF thread starts");

    loop {
        match receive.recv_timeout(POLL) {
            Ok(result) => {
                let _ = worker.join();

                return result.map_err(|failure| match failure {
                    Failure::Typst(message) => RenderError::Typst(message),
                    Failure::Stopped => RenderError::Interrupted,
                });
            }

            Err(mpsc::RecvTimeoutError::Timeout) if host.interrupted() => {
                stop.store(true, Ordering::Relaxed);
                return Err(RenderError::Interrupted);
            }

            Err(mpsc::RecvTimeoutError::Timeout) => {}

            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let payload = worker
                    .join()
                    .expect_err("the thread ended without a result");
                let message = match (
                    payload.downcast_ref::<&str>(),
                    payload.downcast_ref::<String>(),
                ) {
                    (Some(text), _) => (*text).to_owned(),
                    (_, Some(text)) => text.clone(),
                    (None, None) => "no message".to_owned(),
                };

                return Err(RenderError::Panicked(message));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::time::Instant;

    use super::*;

    struct Never;

    impl Signals for Never {
        fn interrupted(&self) -> bool {
            false
        }
    }

    /// Stops on its `after`-th question.
    struct StopAfter {
        asked: Cell<u32>,
        after: u32,
    }

    impl Signals for StopAfter {
        fn interrupted(&self) -> bool {
            self.asked.set(self.asked.get() + 1);
            self.asked.get() >= self.after
        }
    }

    #[test]
    fn renders_a_pdf() {
        let question = Question {
            title: "Title".into(),
            parts: vec![Part::new("Hello $x$.", Role::Answer, Style::Markdown)],
        };

        let pdf = render(vec![question], &Never).unwrap();

        assert!(pdf.starts_with(b"%PDF"));
    }

    #[test]
    fn a_stop_returns_before_the_work_ends() {
        let host = StopAfter {
            asked: Cell::new(0),
            after: 2,
        };
        let start = Instant::now();

        let result = stoppable(&host, |stop| {
            while !stop.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(5));
            }
            Err::<(), _>(Failure::Stopped)
        });

        assert_eq!(result, Err(RenderError::Interrupted));
        assert_eq!(host.asked.get(), 2);
        assert!(start.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn a_panic_becomes_an_error() {
        let result = stoppable(&Never, |_| -> Result<(), Failure> {
            panic!("broken {}", 1)
        });

        assert_eq!(result, Err(RenderError::Panicked("broken 1".into())));
    }

    #[test]
    fn a_typst_failure_keeps_its_message() {
        let result = stoppable(&Never, |_| Err::<(), _>(Failure::Typst("boom".into())));

        assert_eq!(result, Err(RenderError::Typst("boom".into())));
    }
}
