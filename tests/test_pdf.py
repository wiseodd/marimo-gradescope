import builtins
import textwrap
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from marimogs import pdf
from marimogs.config import read_config
from marimogs.pdf import (
    Formula,
    PdfUnavailable,
    cell_markdown,
    find_formulas,
    render_manual_pdf,
)
from marimogs.source import parse_notebook
from marimogs.transform import student_notebook

FIXTURES = Path(__file__).parent / "fixtures"
SOLUTION = FIXTURES / "solution.py"
ANSWER = "Because the order of the terms does not change the sum."

GAUSSIAN = r"""
Let $X \sim \mathcal{N}(\mu, \sigma^2)$ and $Y = aX + b$ with $a \neq 0$. By the change of
variables formula,

$$
p_Y(y) = p_X\!\left(\frac{y - b}{a}\right) \left| \frac{1}{a} \right|
= \frac{1}{|a| \sqrt{2 \pi \sigma^2}} \exp\left( -\frac{(y - (a\mu + b))^2}{2 a^2 \sigma^2} \right),
$$

so $Y \sim \mathcal{N}(a\mu + b, a^2 \sigma^2)$. Hence the Gaussian family is closed. $\blacksquare$
"""

ALIGNED = r"""
**ELBO.** For any $q(z)$,

$$
\begin{aligned}
\log p(x) &= \log \int p(x, z) \, dz \\
&\geq \mathbb{E}_{q(z)}\left[ \log p(x \mid z) \right] -
  \operatorname{KL}\big(q(z) \,\|\, p(z)\big),
\end{aligned}
$$

where the inequality is Jensen's.
"""


def student_copy(path: Path = SOLUTION) -> str:
    src = path.read_text(encoding="utf-8")
    return student_notebook(src, parse_notebook(src), read_config(src, path), "marimo-gradescope")


def md_cell(name: str, text: str) -> str:
    body = textwrap.indent(textwrap.dedent(text).strip(), "    ")
    return f'@app.cell\ndef {name}():\n    mo.md(r"""\n{body}\n    """)\n    return\n\n\n'


def write_notebook(folder: Path, answers: dict[str, str], prompts: dict[str, str] | None = None):
    """A notebook with a prompt and an answer cell per question."""

    cells = "".join(
        md_cell(f"prompt_{qid}", text) for qid, text in (prompts or {}).items()
    ) + "".join(md_cell(f"answer_{qid}", text) for qid, text in answers.items())
    path = folder / "hw.py"
    path.write_text(
        "import marimo\n\napp = marimo.App()\n\nwith app.setup:\n    import marimo as mo\n\n\n"
        f'{cells}if __name__ == "__main__":\n    app.run()\n',
        encoding="utf-8",
    )

    return path


def render(tmp_path: Path, answers: dict[str, str], **kwargs) -> PdfReader:
    notebook = write_notebook(tmp_path, answers, kwargs.pop("prompts", None))
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, list(answers), kwargs.pop("titles", {}), out, **kwargs)

    return PdfReader(BytesIO(out.read_bytes()))


def text_of(reader: PdfReader) -> str:
    return " ".join(" ".join(page.extract_text() for page in reader.pages).split())


# Finding formulas


def spans(text: str) -> list[str]:
    return [text[formula.start : formula.end] for formula in find_formulas(text)]


def test_find_formulas_inline_and_display():
    text = "a $x^2$ b $$\\int f$$ c\n\n$$\ny\n$$"

    assert spans(text) == ["$x^2$", "$$\\int f$$", "$$\ny\n$$"]
    assert [formula.display for formula in find_formulas(text)] == [False, True, True]
    assert find_formulas("a $x^2$")[0] == Formula(start=2, end=7, source="x^2", display=False)


def test_find_formulas_skips_code_and_escaped_dollars():
    text = (
        "Costs \\$5 and \\$6. `$not math$` and ``a ` $no$``.\n\n"
        "```python\nprice = '$x$'\n```\n\n~~~\n$$y$$\n~~~\n\nBut $z$ is."
    )

    assert spans(text) == ["$z$"]


def test_find_formulas_after_an_unclosed_backtick():
    assert spans("Use the ` key.\n\nThen $\\foo$ and `x`.") == ["$\\foo$"]


def test_find_formulas_ignores_an_unclosed_dollar():
    assert spans("Unbalanced: $\\frac{1}{2 end.\n\nNext paragraph $x$.") == ["$x$"]


ALIGN = "\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}"


def test_find_formulas_bare_environment_paragraph():
    text = f"Before.\n\n{ALIGN}\n\nAfter $x$."

    assert spans(text) == [ALIGN, "$x$"]
    assert find_formulas(text)[0] == Formula(
        start=9, end=9 + len(ALIGN), source=ALIGN, display=True, environment=True
    )


@pytest.mark.parametrize(
    "text",
    [
        f"Before.\n{ALIGN}\n\nAfter.",
        f"Before.\n\n{ALIGN}\nAfter.",
        f"Before.\n\n   {ALIGN}\n\nAfter.",
        "\\begin{align}\na &= b\n\nc\n\\end{align}",
        "Text \\begin{equation}x\\end{equation} more.",
        "\\begin{multline}\na\n\\end{multline}",
        "\\begin{align}\na\n\\end{gather}",
        f"```\n{ALIGN}\n```",
        f"$$\n{ALIGN}\n$$",
    ],
)
def test_find_formulas_skips_environments_marimo_shows_as_text(text):
    assert not any(formula.environment for formula in find_formulas(text))


# Markdown of a cell


def test_cell_markdown_of_md_and_code_cells():
    nb = parse_notebook(
        student_copy().replace(
            'def answer_q3():\n    mo.md(r"""', 'def answer_q3():\n    x = 1\n    mo.md(r"""'
        )
    )
    [prompt] = nb.prompt_cells("q3")
    [answer] = nb.answer_cells("q3")

    assert cell_markdown(nb, prompt) == "## Question 3\n\nWhy is addition commutative?"
    assert cell_markdown(nb, answer).startswith("```\n@app.cell\ndef answer_q3():\n    x = 1\n")


# Real PDFs


def test_pdf_has_title_prompt_and_answer(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(), encoding="utf-8")
    out = tmp_path / "answers.pdf"

    assert render_manual_pdf(notebook, ["q3"], {"q3": "Commutativity"}, out) == out
    data = out.read_bytes()
    text = text_of(PdfReader(BytesIO(data)))

    assert data.startswith(b"%PDF")
    assert "Commutativity" in text
    assert "Why is addition commutative?" in text
    assert ANSWER in text
    assert "mo.md" not in text

    # The answer starts its own paragraph, after a rule.
    lines = [line.strip() for line in PdfReader(BytesIO(data)).pages[0].extract_text().splitlines()]
    assert lines[-2:] == ["Why is addition commutative?", ANSWER]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["answers.pdf", "hw.py"]


PART_B = """
@app.cell
def _():
    mo.md(r\"\"\"
    (b) Is subtraction commutative?
    \"\"\")
    return


@app.cell
def _():
    mo.md(r\"\"\"
    <!-- BEGIN SOLUTION -->
    <!-- PROMPT: No, since 2 - 1 differs from 1 - 2. -->
    No.
    <!-- END SOLUTION -->
    \"\"\")
    return

"""


def test_pdf_puts_each_part_s_text_before_its_answer(tmp_path):
    source = tmp_path / "src.py"
    grade = '\n@app.cell\ndef _():\n    mogs.grade("q3", points=5)'
    source.write_text(
        SOLUTION.read_text(encoding="utf-8").replace(grade, "\n" + PART_B + grade),
        encoding="utf-8",
    )
    notebook = tmp_path / "hw.py"
    notebook.write_text(student_copy(source), encoding="utf-8")
    out = tmp_path / "answers.pdf"

    render_manual_pdf(notebook, ["q3"], {"q3": "Commutativity"}, out)
    text = text_of(PdfReader(out))

    order = [
        "Why is addition commutative?",
        ANSWER,
        "(b) Is subtraction commutative?",
        "No, since 2 - 1 differs from 1 - 2.",
    ]
    assert [text.index(part) for part in order] == sorted(text.index(part) for part in order)


def test_math_answers_render_one_page_per_question(tmp_path):
    reader = render(
        tmp_path,
        {"q1": GAUSSIAN, "q2": ALIGNED},
        titles={"q1": "Affine Gaussian", "q2": "Evidence bound"},
    )
    first, second = (" ".join(page.extract_text().split()) for page in reader.pages)

    assert len(reader.pages) == 2
    assert "Affine Gaussian" in first and "closed" in first
    assert "Evidence bound" in second and "Jensen" in second
    assert "\\frac" not in first + second
    assert "\\begin" not in second
    assert "could not be rendered" not in first + second


@pytest.mark.parametrize("env", ["align", "align*", "equation", "equation*", "gather", "gather*"])
@pytest.mark.parametrize("dollars", [False, True])
def test_display_environments_render(tmp_path, env, dollars):
    rows = "a &= b \\\\\nc &= d" if env.startswith("align") else "a = b \\\\\nc = d"
    block = f"\\begin{{{env}}}\n{rows}\n\\end{{{env}}}"
    if dollars:
        block = f"$$\n{block}\n$$"

    text = text_of(render(tmp_path, {"q1": f"Before.\n\n{block}\n\nAfter."}))

    assert "Before. 𝑎 = 𝑏 𝑐 = 𝑑 After." in text, text
    assert "could not be rendered" not in text


def test_gather_star_in_formula_text_keeps_its_star(tmp_path):
    text = text_of(render(tmp_path, {"q1": "Name: $\\text{gather*}$."}))

    assert "gather*" in text, text


def test_formula_shown_as_source_keeps_gather_star(tmp_path):
    formula = "\\iftypst \\begin{gather*} a \\end{gather*}"

    text = text_of(render(tmp_path, {"q1": f"Code: ${formula}$."}))

    assert formula in text, text


def test_failing_environment_is_shown_as_its_source(tmp_path):
    bad = "\\begin{align}\n\\foobar{a} &= b \\\\\nc &= d\n\\end{align}"

    text = text_of(render(tmp_path, {"q1": f"Before.\n\n{bad}\n\nAfter."}))

    assert " ".join(bad.split()) in text
    assert "$$" not in text
    assert "shown as LaTeX source" in text


def test_missing_answer_cell_is_noted(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "First."})
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, ["q1", "q2"], {}, out)
    reader = PdfReader(out)

    assert len(reader.pages) == 2
    assert "No answer cell answer_q2 found" in text_of(reader)


@pytest.mark.parametrize(
    "bad",
    [
        r"$\foobar{x} + \qux$",
        r"$$\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}$$",
    ],
)
def test_failing_formula_is_shown_as_source(tmp_path, bad):
    reader = render(tmp_path, {"q1": f"Good: $\\alpha + \\beta$. Bad: {bad} End of answer."})
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert "End of answer" in text
    assert bad.strip("$").split("{")[0] in text
    assert "𝛼" in text and "\\alpha" not in text
    assert "shown as LaTeX source" in text


BROKEN_HTML = '<table><tr><td rowspan="x">cell</td></tr></table> and $\\alpha$.'


def test_answer_that_defeats_the_math_fallback_is_plain_text(tmp_path):
    reader = render(tmp_path, {"q1": BROKEN_HTML, "q2": "Fine."})
    text = text_of(reader)

    assert len(reader.pages) == 2
    assert BROKEN_HTML in text
    assert "shown as plain text" in text
    assert "Fine." in text


def test_anchor_links_show_their_text(tmp_path):
    text = text_of(render(tmp_path, {"q1": "See [the proof](#nowhere) and $\\alpha$."}))

    assert "See the proof and 𝛼." in text
    assert "could not be rendered" not in text


def test_cells_that_fail_only_together_still_give_a_pdf(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "# Proof\n\nSee [above](#proof)."})
    notebook.write_text(
        notebook.read_text(encoding="utf-8").replace(
            "if __name__", md_cell("answer_q1_more", "# Proof\n\nDone.") + "if __name__"
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, ["q1"], {}, out)
    text = text_of(PdfReader(out))

    assert "See" in text and "Done." in text


def test_lone_surrogate_still_gives_a_pdf(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "x"})
    notebook.write_text(
        notebook.read_text(encoding="utf-8").replace('mo.md(r"""', 'mo.md("""\\ud800 '),
        encoding="utf-8",
    )
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, ["q1"], {}, out)

    assert "x" in text_of(PdfReader(out))


@pytest.mark.parametrize(
    "formula",
    [
        r"$\iftypst #pagebreak() #text[PWNED] \fi$",
        r"$\newcommand{\T}{\iftypst #pagebreak() #text[PWNED] \fi} \T$",
        r'$a \hspace{read("lib.typ")} b$',
        r'$a \vspace{"a" * 40} b$',
        r"$\raisebox{pagebreak()}{x}$",
    ],
)
def test_typst_hidden_in_latex_is_shown_not_run(tmp_path, formula):
    reader = render(tmp_path, {"q1": f"Before {formula} after $\\hspace{{1em}} \\alpha$."})
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert formula.strip("$") in text
    assert "𝛼" in text and "hspace{1em}" not in text
    assert "could not be rendered" not in text


def test_text_commands_in_math_render(tmp_path):
    answer = (
        r"Bold $\mathbf{v} = \textbf{v}$, italic $\textit{w}$, red $\textcolor{red}{x+1}$, "
        r"$\colorbox{yellow}{y}$, $f(x) = 1 \text{ if $x>0$}$, ${\color{blue} z}$, $\emph{e}$."
    )

    text = text_of(render(tmp_path, {"q1": answer}))

    assert "\\" not in text, text
    assert "𝐯" in text


def test_hash_that_mitex_escapes_is_text(tmp_path):
    reader = render(tmp_path, {"q1": r"$\text{x}#pagebreak()$"})

    assert len(reader.pages) == 1
    assert "#𝑝𝑎𝑔𝑒𝑏𝑟𝑒𝑎𝑘()" in text_of(reader)


def test_formula_check_does_not_run_typst():
    inputs = {"formula": r"\iftypst #pagebreak() \fi", "display": "false"}

    assert len(PdfReader(BytesIO(pdf._compile(pdf.FORMULA, inputs))).pages) == 1


def test_typst_in_an_answer_is_shown_not_run(tmp_path):
    answer = "#pagebreak() #set page(width: 1cm) $#pagebreak()$\n\n<!--raw-typst #pagebreak()-->"

    reader = render(
        tmp_path, {"q1": answer}, titles={"q1": '#pagebreak() *Q* $x$ "'}, prompts={"q1": "#a"}
    )
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert '#pagebreak() *Q* $x$ "' in text
    assert "#pagebreak() #set page(width: 1cm)" in text


def test_images_show_their_alt_text(tmp_path):
    answer = 'A ![plot of the loss](loss.png) and <img src="x.png"> and ![](y.png).'

    text = text_of(render(tmp_path, {"q1": answer}))

    assert "[Image: plot of the loss]" in text
    assert text.count("[Image: no description]") == 2


def test_code_answer_and_one_line_cell_are_shown_as_code(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(
        student_copy().replace(
            'def answer_q3():\n    mo.md(r"""\n    Because the order of the terms does not change '
            'the sum.\n    """)\n    return\n',
            'def answer_q3(): mo.md("Because."); return\n',
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, ["q3"], {}, out)

    assert 'def answer_q3(): mo.md("Because."); return' in text_of(PdfReader(out))


def test_status_is_reported(tmp_path):
    messages = []

    render(tmp_path, {"q1": "x"}, status=messages.append)

    assert messages == ["Creating the PDF of your written answers…"]


# Failures


def test_missing_typst_names_the_extra(tmp_path, monkeypatch):
    notebook = write_notebook(tmp_path, {"q1": "x"})
    real = builtins.__import__

    def no_typst(name, *args, **kwargs):
        if name == "typst":
            raise ImportError("No module named 'typst'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_typst)

    with pytest.raises(PdfUnavailable, match=r"marimo-gradescope\[pdf\]"):
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "out.pdf")


def test_unreadable_notebook_gives_pdf_unavailable(tmp_path):
    with pytest.raises(PdfUnavailable, match="cannot be read"):
        render_manual_pdf(tmp_path / "missing.py", ["q1"], {}, tmp_path / "out.pdf")

    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")

    with pytest.raises(PdfUnavailable, match="cannot be read"):
        render_manual_pdf(broken, ["q1"], {}, tmp_path / "out.pdf")


def test_unwritable_output_gives_pdf_unavailable(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "x"})

    with pytest.raises(PdfUnavailable, match="failed"):
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "no" / "such" / "out.pdf")


def test_a_document_that_never_compiles_gives_pdf_unavailable(tmp_path, monkeypatch):
    notebook = write_notebook(tmp_path, {"q1": "x"})
    monkeypatch.setattr(pdf, "TEMPLATE", pdf.TEMPLATE + "\n#panic()\n")

    with pytest.raises(PdfUnavailable, match="Typst"):
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "out.pdf")


# Fonts


def font_names(reader: PdfReader) -> set[str]:
    names = set()

    for page in reader.pages:
        resources = page["/Resources"].get_object()
        assert isinstance(resources, DictionaryObject)
        fonts = resources["/Font"].get_object()
        assert isinstance(fonts, DictionaryObject)

        for font in fonts.values():
            font = font.get_object()
            assert isinstance(font, DictionaryObject)
            names.add(str(font["/BaseFont"]))

    return names


def test_latin_answers_use_only_the_embedded_fonts(tmp_path):
    answer = "Text with `code` and $x^2 + \\alpha$.\n\n```\nprint(1)\n```"

    names = font_names(render(tmp_path, {"q1": answer}))

    assert names
    assert all(
        any(font in name for font in ("LibertinusSerif", "NewCMMath", "DejaVuSansMono"))
        for name in names
    ), names


def test_cjk_and_emoji_do_not_break_the_pdf(tmp_path):
    reader = render(tmp_path, {"q1": "中文答案 🙂 and Latin."})

    assert "and Latin." in text_of(reader)


@pytest.fixture
def font_sets(monkeypatch):
    """The fonts each Typst compile gets, as (system font paths, embedded count)."""

    import typst

    seen = []
    real = typst.compile

    def spy(*args, font_paths, **kwargs):
        fonts = font_paths.fonts()
        paths = [info.path for info in fonts if info.path is not None]
        seen.append((paths, len(fonts) - len(paths)))
        return real(*args, font_paths=font_paths, **kwargs)

    monkeypatch.setattr(typst, "compile", spy)
    return seen


def test_latin_answers_never_see_system_fonts(tmp_path, font_sets):
    # A system font of the same family would replace the embedded one.
    render(tmp_path, {"q1": "Text with `code`, Ünïcödé, Ελληνικά and $\\alpha$ — “quotes”."})

    assert font_sets
    assert all(paths == [] and embedded > 0 for paths, embedded in font_sets)


def test_other_scripts_get_system_fonts_too(tmp_path, font_sets):
    render(tmp_path, {"q1": "中文答案"})

    assert font_sets
    # Every CI runner has some system fonts.
    assert any(paths for paths, _ in font_sets)
    assert pdf._needs_system_fonts("中文答案")
    assert pdf._needs_system_fonts("emoji 🙂")
    assert pdf._needs_system_fonts("עברית")
    assert not pdf._needs_system_fonts("Ünïcödé Ελληνικά Кириллица — “x” 𝛼 ∑ → ≤")


def test_cells_failing_every_rescue_become_plain_text(tmp_path, monkeypatch):
    real = pdf._document
    calls = []

    def flaky(questions):
        calls.append(questions)
        if len(calls) <= 2:
            raise typst_error()
        return real(questions)

    monkeypatch.setattr(pdf, "_rescue", lambda part: part)
    monkeypatch.setattr(pdf, "_document", flaky)

    text = text_of(render(tmp_path, {"q1": "Some *answer*.", "q2": "Other."}))

    assert len(calls) == 3
    assert "Some *answer*." in text
    assert text.count("shown as plain text") == 2


def typst_error():
    import typst

    return typst.TypstError("boom", "")
