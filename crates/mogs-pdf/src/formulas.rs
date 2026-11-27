//! The formulas in a cell's Markdown, as marimo shows them.
//!
//! As in marimo, `\(...\)` and `\[...\]` are math, and a paragraph that is just an `align`,
//! `equation` or `gather` environment, starred or not, is display math too. Offsets are byte
//! offsets. Whitespace and line ends are Python's (`str.isspace`, `str.splitlines`), as in
//! v0.7.

/// The environments that marimo shows as display math without dollar signs.
const ENVIRONMENTS: [&str; 6] = [
    "align",
    "align*",
    "equation",
    "equation*",
    "gather",
    "gather*",
];

/// A `$...$`, `$$...$$`, `\(...\)` or `\[...\]` formula in Markdown, or a bare environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Formula {
    /// Offset of the opening delimiter or `\begin`.
    pub start: usize,
    /// Offset just past the closing delimiter or `\end{...}`.
    pub end: usize,
    /// The LaTeX between the delimiters, or the whole environment.
    pub source: String,
    /// Whether it is display math.
    pub display: bool,
    /// Whether it is a bare environment, which Typst gets inside `$$`.
    pub environment: bool,
    /// Whether it is written `\(...\)` or `\[...\]`, which Typst gets inside `$` or `$$`.
    pub bracket: bool,
}

/// The `$...$`, `$$...$$`, `\(...\)` and `\[...\]` formulas in Markdown, in order.
///
/// Code spans, fenced code blocks and `\$` are skipped. A formula does not span a blank line;
/// a delimiter without a partner is text. A paragraph that is just an `align`, `equation` or
/// `gather` environment, starred or not, is a display formula, as in marimo.
pub fn find_formulas(text: &str) -> Vec<Formula> {
    let bytes = text.as_bytes();
    let mut formulas = Vec::new();

    // Every delimiter is ASCII, so stepping by byte finds what stepping by character finds.
    for (start, end) in prose(text) {
        let mut index = start;

        while index < end {
            match bytes[index] {
                b'\\' => {
                    if let Some(environment) = environment(text, start, end, index) {
                        index = environment.end;
                        formulas.push(environment);
                    } else if bytes[index..].starts_with(b"\\(")
                        || bytes[index..].starts_with(b"\\[")
                    {
                        let display = bytes[index + 1] == b'[';
                        let closing = if display { "\\]" } else { "\\)" };

                        match closing_at(text, index + 2, end, closing) {
                            None => index += 2,
                            Some(close) => {
                                formulas.push(Formula {
                                    start: index,
                                    end: close + 2,
                                    source: text[index + 2..close].to_owned(),
                                    display,
                                    environment: false,
                                    bracket: true,
                                });
                                index = close + 2;
                            }
                        }
                    } else {
                        index += 2;
                    }
                }

                b'`' => {
                    let run = bytes[index..end].iter().take_while(|b| **b == b'`').count();
                    let limit = blank_line(text, index, end).unwrap_or(end);

                    index = match backtick_run(bytes, index + run, limit, run) {
                        Some(close) => close + run,
                        None => index + run,
                    };
                }

                b'$' => {
                    let width = if bytes[index..].starts_with(b"$$") {
                        2
                    } else {
                        1
                    };
                    let closing = &"$$"[..width];

                    match closing_at(text, index + width, end, closing) {
                        None => index += width,
                        Some(close) => {
                            formulas.push(Formula {
                                start: index,
                                end: close + width,
                                source: text[index + width..close].to_owned(),
                                display: width == 2,
                                environment: false,
                                bracket: false,
                            });
                            index = close + width;
                        }
                    }
                }

                _ => index += 1,
            }
        }
    }

    formulas
}

/// `text` with its formulas in dollar signs, so cmarker hands them to mitex.
///
/// A bare environment goes inside `$$`; `\(...\)` and `\[...\]` become `$...$` and `$$...$$`.
pub fn delimited(text: &str) -> String {
    let mut text = text.to_owned();

    for formula in find_formulas(&text).into_iter().rev() {
        let (before, after) = (&text[..formula.start], &text[formula.end..]);

        if formula.environment {
            text = format!("{before}$$\n{}\n$$\n{after}", formula.source);
        } else if formula.bracket {
            let dollars = if formula.display { "$$" } else { "$" };
            text = format!("{before}{dollars}{}{dollars}{after}", formula.source);
        }
    }

    text
}

/// The formula's source with its dollar signs, as a code block or a code span.
pub fn as_code(text: &str, formula: &Formula) -> String {
    let source = &text[formula.start..formula.end];

    if formula.display {
        return format!("\n\n{}\n\n", code_block(source));
    }

    let fence = "`".repeat(longest_backtick_run(source) + 1);
    let words: Vec<&str> = source
        .split(is_python_space)
        .filter(|word| !word.is_empty())
        .collect();

    format!("{fence} {} {fence}", words.join(" "))
}

/// A fenced code block holding `text` verbatim; the fence outgrows every backtick run.
pub fn code_block(text: &str) -> String {
    let fence = "`".repeat((longest_backtick_run(text) + 1).max(3));

    format!("{fence}\n{text}\n{fence}")
}

/// Whether `text` has characters from scripts Typst's embedded fonts do not cover.
///
/// A heuristic by Unicode block: Latin, Greek, Cyrillic, punctuation, arrows and math are
/// covered; Hebrew to the Indic and South-East Asian scripts, CJK and emoji are not.
pub fn needs_system_fonts(text: &str) -> bool {
    text.chars().map(u32::from).any(|point| {
        (0x0590..0x1D00).contains(&point)
            || (point >= 0x2E80 && !(0x1D400..0x1D800).contains(&point))
    })
}

/// The bare environment whose paragraph starts at `index`, if there is one.
///
/// `start` and `end` bound the run of prose that holds `index`.
fn environment(text: &str, start: usize, end: usize, index: usize) -> Option<Formula> {
    if !text[index..].starts_with("\\begin{") || !paragraph_starts(text, start, index) {
        return None;
    }

    let stop = blank_line(text, index, end).unwrap_or(end);
    let paragraph = text[index..stop].trim_end_matches(is_python_space);

    if !is_environment(paragraph) {
        return None;
    }

    Some(Formula {
        start: index,
        end: index + paragraph.len(),
        source: paragraph.to_owned(),
        display: true,
        environment: true,
        bracket: false,
    })
}

/// Whether `paragraph` is `\begin{name}...\end{name}` for one of `ENVIRONMENTS`.
fn is_environment(paragraph: &str) -> bool {
    ENVIRONMENTS.iter().any(|name| {
        let begin = format!("\\begin{{{name}}}");
        let end = format!("\\end{{{name}}}");

        paragraph
            .strip_prefix(&begin)
            .is_some_and(|rest| rest.ends_with(&end))
    })
}

/// Whether a paragraph starts at `index`: the prose start, or after a blank line.
///
/// Only the line before `index` is read.
fn paragraph_starts(text: &str, start: usize, index: usize) -> bool {
    if index == start {
        return true;
    }

    if text.as_bytes()[index - 1] != b'\n' {
        return false;
    }

    let line_start = text[start..index - 1]
        .rfind('\n')
        .map_or(start, |at| start + at + 1);

    text[line_start..index - 1]
        .bytes()
        .all(|b| b == b' ' || b == b'\t')
}

/// Offsets of the runs of lines outside fenced code blocks.
fn prose(text: &str) -> Vec<(usize, usize)> {
    let mut runs = Vec::new();
    let mut fence: Option<&str> = None;
    let mut start = 0;
    let mut offset = 0;

    for line in python_lines(text) {
        let stripped = line.trim_matches(is_python_space);

        match (fence, opening_fence(line)) {
            (None, Some(opened)) => {
                runs.push((start, offset));
                fence = Some(opened);
            }
            (Some(open), _) if closes(stripped, open) => {
                fence = None;
                start = offset + line.len();
            }
            _ => {}
        }

        offset += line.len();
    }

    if fence.is_none() {
        runs.push((start, text.len()));
    }

    runs.retain(|(begin, end)| begin < end);
    runs
}

/// The fence a line opens with: up to three spaces, then three or more `` ` `` or `~`.
fn opening_fence(line: &str) -> Option<&str> {
    let rest = line.trim_start_matches(' ');

    if line.len() - rest.len() > 3 {
        return None;
    }

    let mark = rest.chars().next().filter(|c| *c == '`' || *c == '~')?;
    let run = rest.len() - rest.trim_start_matches(mark).len();

    (run >= 3).then(|| &rest[..run])
}

/// Whether a stripped line closes `fence`: only its character, at least as many.
fn closes(stripped: &str, fence: &str) -> bool {
    let mark = fence.as_bytes()[0];

    !stripped.is_empty() && stripped.bytes().all(|b| b == mark) && stripped.len() >= fence.len()
}

/// Offset of the `delimiter` that closes a formula opened before `start`, if any.
fn closing_at(text: &str, start: usize, end: usize, delimiter: &str) -> Option<usize> {
    let bytes = text.as_bytes();
    let mut index = start;

    while index < end {
        // The delimiter first: `\)` and `\]` start with the escape character.
        if bytes[index..].starts_with(delimiter.as_bytes()) {
            return Some(index);
        } else if bytes[index] == b'\\' {
            index += 2;
        } else if blank_line_at(bytes, index, bytes.len()).is_some() {
            return None;
        } else {
            index += 1;
        }
    }

    None
}

/// Offset of the first blank line (`\n[ \t]*\n`) that lies within `from..to`.
fn blank_line(text: &str, from: usize, to: usize) -> Option<usize> {
    let bytes = text.as_bytes();

    (from..to).find(|at| blank_line_at(bytes, *at, to).is_some())
}

/// The end of a blank line (`\n[ \t]*\n`) that starts at `at` and ends by `to`, if any.
fn blank_line_at(bytes: &[u8], at: usize, to: usize) -> Option<usize> {
    if bytes.get(at) != Some(&b'\n') {
        return None;
    }

    let spaces = bytes[at + 1..to]
        .iter()
        .take_while(|b| **b == b' ' || **b == b'\t')
        .count();
    let close = at + 1 + spaces;

    (close < to && bytes[close] == b'\n').then_some(close + 1)
}

/// Offset of a run of exactly `run` backticks in `from..limit`, as `(?<!`)`{run}(?!`)` finds it.
///
/// A run that reaches `limit` ends there, as a regex search stops at its end position.
fn backtick_run(bytes: &[u8], from: usize, limit: usize, run: usize) -> Option<usize> {
    let mut index = from;

    while index < limit {
        if bytes[index] != b'`' {
            index += 1;
            continue;
        }

        let length = bytes[index..limit]
            .iter()
            .take_while(|b| **b == b'`')
            .count();
        let after_backtick = index > 0 && bytes[index - 1] == b'`';

        if length == run && !after_backtick {
            return Some(index);
        }

        index += length;
    }

    None
}

/// The length of the longest run of backticks in `text`.
fn longest_backtick_run(text: &str) -> usize {
    text.split(|c| c != '`').map(str::len).max().unwrap_or(0)
}

/// Python's `str.isspace` for one character.
fn is_python_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// The lines of `text` with their ends, as Python's `str.splitlines(keepends=True)`.
fn python_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();

    while let Some((at, c)) = chars.next() {
        if !matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            continue;
        }

        let mut end = at + c.len_utf8();

        if c == '\r' && chars.next_if(|(_, next)| *next == '\n').is_some() {
            end += 1;
        }

        lines.push(&text[start..end]);
        start = end;
    }

    if start < text.len() {
        lines.push(&text[start..]);
    }

    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spans(text: &str) -> Vec<&str> {
        find_formulas(text)
            .into_iter()
            .map(|formula| &text[formula.start..formula.end])
            .collect()
    }

    fn displays(text: &str) -> Vec<bool> {
        find_formulas(text)
            .into_iter()
            .map(|formula| formula.display)
            .collect()
    }

    const ALIGN: &str = "\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}";

    #[test]
    fn inline_and_display() {
        let text = "a $x^2$ b $$\\int f$$ c\n\n$$\ny\n$$";

        assert_eq!(spans(text), ["$x^2$", "$$\\int f$$", "$$\ny\n$$"]);
        assert_eq!(displays(text), [false, true, true]);
        assert_eq!(
            find_formulas("a $x^2$")[0],
            Formula {
                start: 2,
                end: 7,
                source: "x^2".into(),
                display: false,
                environment: false,
                bracket: false,
            }
        );
    }

    #[test]
    fn skips_code_and_escaped_dollars() {
        let text = "Costs \\$5 and \\$6. `$not math$` and ``a ` $no$``.\n\n\
                    ```python\nprice = '$x$'\n```\n\n~~~\n$$y$$\n~~~\n\nBut $z$ is.";

        assert_eq!(spans(text), ["$z$"]);
    }

    #[test]
    fn after_an_unclosed_backtick() {
        assert_eq!(
            spans("Use the ` key.\n\nThen $\\foo$ and `x`."),
            ["$\\foo$"]
        );
    }

    #[test]
    fn ignores_an_unclosed_dollar() {
        assert_eq!(
            spans("Unbalanced: $\\frac{1}{2 end.\n\nNext paragraph $x$."),
            ["$x$"]
        );
    }

    #[test]
    fn backslash_brackets() {
        let text = "a \\(x^2\\) b \\[\\int f\\] c\n\n\\[\ny\n\\]";

        assert_eq!(spans(text), ["\\(x^2\\)", "\\[\\int f\\]", "\\[\ny\n\\]"]);
        assert_eq!(displays(text), [false, true, true]);
        assert_eq!(
            find_formulas("a \\(x^2\\)")[0],
            Formula {
                start: 2,
                end: 9,
                source: "x^2".into(),
                display: false,
                environment: false,
                bracket: true,
            }
        );
    }

    #[test]
    fn skips_backslash_brackets_that_are_not_math() {
        for text in [
            "`\\(x\\)` in code",
            "\\(x unclosed",
            "\\(x\n\ny\\)",
            "```\n\\[x\\]\n```",
        ] {
            assert_eq!(spans(text), Vec::<&str>::new(), "{text:?}");
        }
    }

    #[test]
    fn bare_environment_paragraph() {
        let text = format!("Before.\n\n{ALIGN}\n\nAfter $x$.");

        assert_eq!(spans(&text), [ALIGN, "$x$"]);
        assert_eq!(
            find_formulas(&text)[0],
            Formula {
                start: 9,
                end: 9 + ALIGN.len(),
                source: ALIGN.into(),
                display: true,
                environment: true,
                bracket: false,
            }
        );
    }

    #[test]
    fn skips_environments_marimo_shows_as_text() {
        for text in [
            format!("Before.\n{ALIGN}\n\nAfter."),
            format!("Before.\n\n{ALIGN}\nAfter."),
            format!("Before.\n\n   {ALIGN}\n\nAfter."),
            "\\begin{align}\na &= b\n\nc\n\\end{align}".into(),
            "Text \\begin{equation}x\\end{equation} more.".into(),
            "\\begin{multline}\na\n\\end{multline}".into(),
            "\\begin{align}\na\n\\end{gather}".into(),
            format!("```\n{ALIGN}\n```"),
            format!("$$\n{ALIGN}\n$$"),
        ] {
            assert!(
                !find_formulas(&text).iter().any(|f| f.environment),
                "{text:?}"
            );
        }
    }

    #[test]
    fn many_backslashes_are_found_fast() {
        let text = format!("{}\n\n{ALIGN}", "Some \\alpha and ".repeat(200_000));

        assert_eq!(spans(&text).last(), Some(&ALIGN));
    }

    #[test]
    fn offsets_are_bytes_around_wide_characters() {
        let text = "é \\é $ü$ 中 \\(x\\)";

        assert_eq!(spans(text), ["$ü$", "\\(x\\)"]);
    }

    #[test]
    fn python_line_ends_bound_fences() {
        // `\u{2028}` ends a line for Python, so the fence closes before `$x$`.
        assert_eq!(spans("```\u{2028}code\u{2028}```\u{2028}$x$"), ["$x$"]);
        // A closing fence may carry Python whitespace such as `\u{1f}`.
        assert_eq!(spans("```\n$y$\n```\u{1f}\n$x$"), ["$x$"]);
    }

    #[test]
    fn delimited_wraps_environments_and_brackets() {
        assert_eq!(
            delimited(&format!("A\n\n{ALIGN}\n\nB \\(x\\) \\[y\\]")),
            format!("A\n\n$$\n{ALIGN}\n$$\n\n\nB $x$ $$y$$")
        );
    }

    #[test]
    fn as_code_of_inline_and_display_formulas() {
        let text = "a $x  ` \u{1f}y$ b";
        let formula = &find_formulas(text)[0];

        assert_eq!(as_code(text, formula), "`` $x ` y$ ``");

        let text = "$$ ``` $$";
        let formula = &find_formulas(text)[0];

        assert_eq!(as_code(text, formula), "\n\n````\n$$ ``` $$\n````\n\n");
    }

    #[test]
    fn system_fonts_only_for_scripts_the_embedded_fonts_lack() {
        assert!(needs_system_fonts("中文答案"));
        assert!(needs_system_fonts("emoji 🙂"));
        assert!(needs_system_fonts("עברית"));
        assert!(!needs_system_fonts(
            "Ünïcödé Ελληνικά Кириллица — “x” 𝛼 ∑ → ≤"
        ));
    }

    #[test]
    fn python_whitespace_and_lines() {
        assert!(is_python_space('\u{1c}') && is_python_space('\u{a0}'));
        assert!(!is_python_space('\u{200b}'));
        assert_eq!(
            python_lines("a\r\nb\rc\u{85}d\u{0b}e"),
            ["a\r\n", "b\r", "c\u{85}", "d\u{0b}", "e"]
        );
    }
}
