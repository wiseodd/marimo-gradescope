//! Errors located in a notebook, drawn the way rustc draws its errors.
//!
//! Python finds the place of an error; this module only reads the lines it names and draws
//! them with `annotate-snippets`.

use std::ops::Range;

use annotate_snippets::{AnnotationKind, Group, Level, Renderer, Snippet};

/// An error and where it is in a file.
///
/// Lines are 1-based. Columns are 0-based offsets in UTF-8 bytes, as Python's `ast` gives
/// them, and `end_col` is exclusive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Located {
    /// The error, without the line it names.
    pub message: String,
    /// The file, as other messages show it.
    pub path: String,
    /// First line.
    pub line: usize,
    /// Start in the first line.
    pub col: usize,
    /// Last line.
    pub end_line: usize,
    /// End in the last line.
    pub end_col: usize,
    /// A short note at the span.
    pub label: Option<String>,
    /// A fix, on its own `help:` line.
    pub help: Option<String>,
}

/// Draw `located` on `source`, the text of its file, with ANSI colors if `color`.
///
/// Returns `None` when the span does not fit `source`, as when the file changed after Python
/// read it. The caller then prints the error's plain text.
#[must_use]
pub fn render(located: &Located, source: &str, color: bool) -> Option<String> {
    let lines = lines(source);
    let (text, span) = snippet(&lines, located)?;

    let mut annotation = AnnotationKind::Primary.span(span);
    if let Some(label) = &located.label {
        annotation = annotation.label(label.as_str());
    }

    let snippet = Snippet::source(text)
        .line_start(located.line)
        .path(located.path.as_str())
        .annotation(annotation);
    let mut report = vec![
        Level::ERROR
            .primary_title(located.message.as_str())
            .element(snippet),
    ];
    if let Some(help) = &located.help {
        report.push(Group::with_title(
            Level::HELP.secondary_title(help.as_str()),
        ));
    }

    let renderer = if color {
        Renderer::styled()
    } else {
        Renderer::plain()
    };

    Some(renderer.render(&report))
}

/// The lines of `source`, split at `\r\n`, `\r` and `\n` as Python numbers them.
fn lines(source: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut rest = source;

    while let Some(end) = rest.find(['\r', '\n']) {
        lines.push(&rest[..end]);
        let skip = if rest[end..].starts_with("\r\n") {
            2
        } else {
            1
        };
        rest = &rest[end + skip..];
    }
    lines.push(rest);

    lines
}

/// The lines `located` spans, joined by `\n`, and its byte range in them.
fn snippet(lines: &[&str], located: &Located) -> Option<(String, Range<usize>)> {
    let Located {
        line,
        col,
        end_line,
        end_col,
        ..
    } = *located;

    if line == 0 || end_line < line || end_line > lines.len() {
        return None;
    }

    let spanned = &lines[line - 1..end_line];
    let (first, last) = (spanned[0], spanned[spanned.len() - 1]);

    if !first.is_char_boundary(col) || !last.is_char_boundary(end_col) {
        return None;
    }

    let text = spanned.join("\n");
    let end = text.len() - last.len() + end_col;

    if end < col {
        return None;
    }

    Some((text, col..end))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn located(line: usize, col: usize, end_line: usize, end_col: usize) -> Located {
        Located {
            message: "`points` must be a number >= 0, got '10'.".to_owned(),
            path: "hw/hw.py".to_owned(),
            line,
            col,
            end_line,
            end_col,
            label: None,
            help: None,
        }
    }

    #[test]
    fn draws_the_line_label_and_help() {
        let source = "import mogs\n\nmogs.grade(\"q1\", points=\"10\")\n";
        let error = Located {
            label: Some("a string".to_owned()),
            help: Some("drop the quotes: points=10".to_owned()),
            ..located(3, 24, 3, 28)
        };

        assert_eq!(
            render(&error, source, false).unwrap(),
            "\
error: `points` must be a number >= 0, got '10'.
 --> hw/hw.py:3:25
  |
3 | mogs.grade(\"q1\", points=\"10\")
  |                         ^^^^ a string
  |
help: drop the quotes: points=10"
        );
    }

    #[test]
    fn a_tab_before_the_span_keeps_the_caret_under_it() {
        let source = "\tx = \"10\"\n";

        assert_eq!(
            render(&located(1, 5, 1, 9), source, false).unwrap(),
            "\
error: `points` must be a number >= 0, got '10'.
 --> hw/hw.py:1:6
  |
1 |     x = \"10\"
  |         ^^^^"
        );
    }

    #[test]
    fn non_ascii_before_the_span_keeps_the_caret_under_it() {
        let source = "é = \"日本\"; x = \"10\"\n";
        let col = "é = \"日本\"; x = ".len();

        assert_eq!(
            render(&located(1, col, 1, col + 4), source, false).unwrap(),
            "\
error: `points` must be a number >= 0, got '10'.
 --> hw/hw.py:1:15
  |
1 | é = \"日本\"; x = \"10\"
  |                 ^^^^"
        );
    }

    #[test]
    fn spans_several_lines() {
        let source = "a\nx = (1,\n     2)\n";

        assert_eq!(
            render(&located(2, 4, 3, 7), source, false).unwrap(),
            "\
error: `points` must be a number >= 0, got '10'.
 --> hw/hw.py:2:5
  |
2 |   x = (1,
  |  _____^
3 | |      2)
  | |_______^"
        );
    }

    #[test]
    fn splits_lines_at_crlf_and_cr() {
        assert_eq!(lines("a\r\nb\rc\nd"), ["a", "b", "c", "d"]);
        assert_eq!(lines("a\n"), ["a", ""]);
    }

    #[test]
    fn a_span_outside_the_source_draws_nothing() {
        let source = "x = 1\n";

        assert_eq!(render(&located(3, 0, 3, 1), source, false), None);
        assert_eq!(render(&located(1, 0, 1, 9), source, false), None);
        assert_eq!(render(&located(1, 3, 1, 1), source, false), None);
        assert_eq!(render(&located(0, 0, 1, 1), source, false), None);
        assert_eq!(render(&located(1, 1, 1, 2), "é\n", false), None);
    }

    #[test]
    fn colors_only_when_asked() {
        let source = "x = \"10\"\n";

        assert!(
            render(&located(1, 4, 1, 8), source, true)
                .unwrap()
                .contains("\x1b[")
        );
        assert!(
            !render(&located(1, 4, 1, 8), source, false)
                .unwrap()
                .contains('\x1b')
        );
    }
}
