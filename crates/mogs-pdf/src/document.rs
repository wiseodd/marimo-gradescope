//! The document of the questions, and the fallbacks for cells that break it.
//!
//! The cell text reaches Typst as data in `sys.inputs`, never as Typst source. Typst cannot
//! recover from an error inside a document, so a cell that breaks the document is rendered
//! again with less: first with each failing formula shown as LaTeX source, then as plain text.
//! A note under the cell says so.

use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;

use crate::formulas::{Formula, as_code, delimited, find_formulas};
use crate::world;
use crate::{Part, Question, RenderError, Style};

/// The packages and math helpers both documents start with.
///
/// mitex evaluates the Typst it converts LaTeX to, so a formula that could run Typst code
/// shows as source: `\iftypst` passes Typst through, `\hspace`, `\vspace` and `\raisebox`
/// evaluate their first argument (only a plain length is let through), and a `#` left in the
/// converted formula starts code. mitex itself writes escaped `\#` and the calls in `aliases`
/// (from `\text`, `\textbf`, `\textcolor`, `\color`, `$...$` inside `\text`, ...), which are
/// dropped before the check. Other calls it writes (`\section`, `\footnote`, `\ref`, ...)
/// stay flagged, so those formulas show as source.
macro_rules! preamble {
    () => {
        include_str!("preamble.typ")
    };
}

/// The document. `sys.inputs.doc` is JSON: a list of questions, each `{title, parts}`, where
/// a part is `{text, role, style, note}`. Images are not loaded: they show as their alt text.
const TEMPLATE: &str = concat!(preamble!(), include_str!("template.typ"));

/// One formula alone, as cmarker hands it to mitex.
const FORMULA: &str = concat!(
    preamble!(),
    "#safe-math(sys.inputs.formula, block: sys.inputs.display == \"true\")\n"
);

/// The note under a cell whose failing formulas show as LaTeX source.
pub const SOURCE_NOTE: &str =
    "Some math in this cell could not be rendered and is shown as LaTeX source.";

/// The note under a cell that shows as plain text.
pub const PLAIN_NOTE: &str = "This cell could not be rendered and is shown as plain text.";

/// Typst runs that end early once `stop` is set.
pub(crate) struct Typst<'a> {
    pub(crate) stop: &'a AtomicBool,
}

impl Typst<'_> {
    /// The PDF of `questions`; parts that break the document are rendered with less.
    pub(crate) fn pdf(&self, questions: &[Question]) -> Result<Vec<u8>, RenderError> {
        fallbacks(
            questions,
            |questions| self.document(questions),
            |part| self.rescue(part),
        )
    }

    /// The PDF of `questions` as they are.
    fn document(&self, questions: &[Question]) -> Result<Vec<u8>, RenderError> {
        self.go_on()?;

        world::compile("main.typ", TEMPLATE, &[("doc", &doc(questions))])
            .map_err(RenderError::Typst)
    }

    /// `part`, or a version of it that Typst can render, with a note when it changed.
    fn rescue(&self, part: &Part) -> Result<Part, RenderError> {
        if self.renders(part)? {
            return Ok(part.clone());
        }

        let mut text = part.text.clone();

        for formula in find_formulas(&part.text).into_iter().rev() {
            if !self.formula_renders(&formula)? {
                let code = as_code(&text, &formula);
                text.replace_range(formula.start..formula.end, &code);
            }
        }

        let shown = Part {
            text,
            style: Style::Markdown,
            note: Some(SOURCE_NOTE),
            ..part.clone()
        };

        if shown.text != part.text && self.renders(&shown)? {
            return Ok(shown);
        }

        Ok(as_plain(part))
    }

    /// Whether `part` alone, under an empty title, renders. The PDF is made too, as a part
    /// can lay out and still fail to export.
    fn renders(&self, part: &Part) -> Result<bool, RenderError> {
        let question = Question {
            title: String::new(),
            parts: vec![part.clone()],
        };

        succeeded(self.document(&[question]))
    }

    fn formula_renders(&self, formula: &Formula) -> Result<bool, RenderError> {
        let display = if formula.display { "true" } else { "false" };
        let inputs = [("formula", formula.source.as_str()), ("display", display)];
        self.go_on()?;

        succeeded(world::compile("formula.typ", FORMULA, &inputs).map_err(RenderError::Typst))
    }

    /// `Err(Interrupted)` once the caller stopped the work, so no more Typst runs start.
    fn go_on(&self) -> Result<(), RenderError> {
        match self.stop.load(Ordering::Relaxed) {
            true => Err(RenderError::Interrupted),
            false => Ok(()),
        }
    }
}

/// `questions` as the template's `sys.inputs.doc`.
fn doc(questions: &[Question]) -> String {
    let doc: Vec<DocQuestion<'_>> = questions.iter().map(DocQuestion::of).collect();

    serde_json::to_string(&doc).expect("the document is plain JSON")
}

/// The document of `questions`, else of the rescued parts, else of plain text.
///
/// Cells can also break the document only together, as with two headings of the same name and
/// a link to them: the plain-text document follows the rescued one then.
fn fallbacks(
    questions: &[Question],
    mut document: impl FnMut(&[Question]) -> Result<Vec<u8>, RenderError>,
    mut rescue: impl FnMut(&Part) -> Result<Part, RenderError>,
) -> Result<Vec<u8>, RenderError> {
    if let Some(pdf) = unless_typst_failed(document(questions))? {
        return Ok(pdf);
    }

    let mut rescued = Vec::with_capacity(questions.len());

    for question in questions {
        let parts = question
            .parts
            .iter()
            .map(&mut rescue)
            .collect::<Result<_, _>>()?;
        rescued.push(Question {
            parts,
            ..question.clone()
        });
    }

    if let Some(pdf) = unless_typst_failed(document(&rescued))? {
        return Ok(pdf);
    }

    let plain: Vec<Question> = questions
        .iter()
        .map(|question| Question {
            parts: question.parts.iter().map(as_plain).collect(),
            ..question.clone()
        })
        .collect();

    document(&plain)
}

/// Whether Typst made the PDF; any error but Typst's stays one.
fn succeeded(result: Result<Vec<u8>, RenderError>) -> Result<bool, RenderError> {
    unless_typst_failed(result).map(|pdf| pdf.is_some())
}

/// The PDF, or `None` when Typst failed; any other error stays one.
fn unless_typst_failed(
    result: Result<Vec<u8>, RenderError>,
) -> Result<Option<Vec<u8>>, RenderError> {
    match result {
        Ok(pdf) => Ok(Some(pdf)),
        Err(RenderError::Typst(_)) => Ok(None),
        Err(error) => Err(error),
    }
}

fn as_plain(part: &Part) -> Part {
    match part.style {
        Style::Note => part.clone(),
        Style::Markdown | Style::Plain => Part {
            style: Style::Plain,
            note: Some(PLAIN_NOTE),
            ..part.clone()
        },
    }
}

/// A question as the template reads it.
#[derive(Serialize)]
struct DocQuestion<'a> {
    title: &'a str,
    parts: Vec<DocPart<'a>>,
}

#[derive(Serialize)]
struct DocPart<'a> {
    text: String,
    role: crate::Role,
    style: Style,
    note: Option<&'a str>,
}

impl<'a> DocQuestion<'a> {
    fn of(question: &'a Question) -> Self {
        let parts = question
            .parts
            .iter()
            .map(|part| DocPart {
                text: match part.style {
                    Style::Markdown => delimited(&part.text),
                    Style::Plain | Style::Note => part.text.clone(),
                },
                role: part.role,
                style: part.style,
                note: part.note,
            })
            .collect();

        Self {
            title: &question.title,
            parts,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Role;

    fn question(texts: &[&str]) -> Question {
        Question {
            title: "Q".into(),
            parts: texts
                .iter()
                .map(|text| Part::new(*text, Role::Answer, Style::Markdown))
                .collect(),
        }
    }

    #[test]
    #[ignore = "needs network: downloads the Typst packages on a cold cache"]
    fn formula_check_does_not_run_typst() {
        let inputs = [
            ("formula", r"\iftypst #pagebreak() \fi"),
            ("display", "false"),
        ];

        let document = world::layout("formula.typ", FORMULA, &inputs).unwrap();

        // `#pagebreak()` would make two pages.
        assert_eq!(document.pages().len(), 1);
    }

    #[test]
    fn cells_failing_every_rescue_become_plain_text() {
        let mut calls: Vec<Vec<Question>> = Vec::new();

        let pdf = fallbacks(
            &[question(&["Some *answer*."]), question(&["Other."])],
            |questions| {
                calls.push(questions.to_vec());
                match calls.len() {
                    1 | 2 => Err(RenderError::Typst("boom".into())),
                    _ => Ok(b"%PDF".to_vec()),
                }
            },
            |part| Ok(part.clone()),
        );

        assert_eq!(pdf, Ok(b"%PDF".to_vec()));
        assert_eq!(calls.len(), 3);
        let plain: Vec<&Part> = calls[2].iter().flat_map(|q| &q.parts).collect();
        assert!(
            plain
                .iter()
                .all(|part| part.style == Style::Plain && part.note == Some(PLAIN_NOTE))
        );
        assert_eq!(plain[0].text, "Some *answer*.");
    }

    #[test]
    fn a_document_that_never_compiles_fails_with_typst_s_message() {
        let result = fallbacks(
            &[question(&["x"])],
            |_| Err(RenderError::Typst("unknown variable: boom".into())),
            |part| Ok(part.clone()),
        );

        assert_eq!(
            result,
            Err(RenderError::Typst("unknown variable: boom".into()))
        );
    }

    #[test]
    fn a_stop_ends_the_fallbacks() {
        let mut calls = 0;

        let result = fallbacks(
            &[question(&["x"])],
            |_| {
                calls += 1;
                Err(RenderError::Typst("boom".into()))
            },
            |_| Err(RenderError::Interrupted),
        );

        assert_eq!(result, Err(RenderError::Interrupted));
        assert_eq!(calls, 1);
    }

    #[test]
    fn a_set_stop_runs_no_typst() {
        let stop = AtomicBool::new(true);

        let result = Typst { stop: &stop }.pdf(&[question(&["x"])]);

        assert_eq!(result, Err(RenderError::Interrupted));
    }

    #[test]
    fn notes_stay_notes_in_plain_text() {
        let note = Part::new("No answer.", Role::Answer, Style::Note);

        assert_eq!(as_plain(&note), note);
    }

    #[test]
    #[ignore = "needs network: downloads the Typst packages on a cold cache"]
    fn failing_formula_is_rescued_as_source() {
        let stop = AtomicBool::new(false);
        let part = Part::new(
            "Good $x$. Bad $\\foobar{x}$.",
            Role::Answer,
            Style::Markdown,
        );

        let rescued = Typst { stop: &stop }.rescue(&part).unwrap();

        assert_eq!(rescued.text, "Good $x$. Bad ` $\\foobar{x}$ `.");
        assert_eq!(rescued.note, Some(SOURCE_NOTE));
    }
}
