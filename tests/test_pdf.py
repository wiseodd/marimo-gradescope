import _thread
import textwrap
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from mogs import _answers, _native
from mogs._answers import (
    Part,
    PdfUnavailable,
    Question,
    Role,
    Style,
    cell_markdown,
    render_manual_pdf,
)
from mogs.config import read_config
from mogs.source import parse_notebook
from mogs.transform import student_notebook

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
    return student_notebook(src, parse_notebook(src), read_config(src, path), "mogs")


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


@pytest.mark.network
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


@pytest.mark.parametrize(
    ("title", "heading"),
    [
        ("Question 4: Derive the slope", "### Derive the slope"),
        ("Question 4", "## Question 4"),
        ("Derive the slope", "## Derive the Slope."),
    ],
)
@pytest.mark.network
def test_prompt_heading_the_title_already_says_is_dropped(tmp_path, title, heading):
    reader = render(
        tmp_path,
        {"q4": "It is 2."},
        prompts={"q4": f"{heading}\n\nFind the slope of $y = 2x$."},
        titles={"q4": title},
    )

    text = text_of(reader)
    shown = heading.lstrip("#").strip().rstrip(".").casefold()
    assert text.casefold().count(shown) == 1
    assert "Find the slope" in text


@pytest.mark.parametrize(
    ("title", "heading"),
    [
        ("Question 4", "Question 4: Derive the slope"),
        ("Question 10", "Question 1"),
        ("Question 4: x + y", "x - y"),
    ],
)
@pytest.mark.network
def test_prompt_heading_with_more_than_the_title_stays(tmp_path, title, heading):
    reader = render(
        tmp_path,
        {"q4": "It is 2."},
        prompts={"q4": f"## {heading}\n\nFind it."},
        titles={"q4": title},
    )

    assert f"{title} {heading}" in text_of(reader)


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


@pytest.mark.network
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


@pytest.mark.network
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
@pytest.mark.network
def test_display_environments_render(tmp_path, env, dollars):
    rows = "a &= b \\\\\nc &= d" if env.startswith("align") else "a = b \\\\\nc = d"
    block = f"\\begin{{{env}}}\n{rows}\n\\end{{{env}}}"
    if dollars:
        block = f"$$\n{block}\n$$"

    text = text_of(render(tmp_path, {"q1": f"Before.\n\n{block}\n\nAfter."}))

    assert "Before. 𝑎 = 𝑏 𝑐 = 𝑑 After." in text, text
    assert "could not be rendered" not in text


@pytest.mark.network
def test_backslash_bracket_math_renders(tmp_path):
    answer = "Inline \\(a = b\\) here.\n\n\\[\nc = d\n\\]\n\nAfter."

    text = text_of(render(tmp_path, {"q1": answer}))

    assert "Inline 𝑎 = 𝑏 here." in text, text
    assert "𝑐 = 𝑑" in text, text
    assert "\\(" not in text and "\\[" not in text
    assert "could not be rendered" not in text


@pytest.mark.network
def test_failing_backslash_bracket_formula_is_shown_as_its_source(tmp_path):
    formula = "\\iftypst \\begin{gather*} a \\end{gather*}"

    text = text_of(render(tmp_path, {"q1": f"Code: \\({formula}\\) and \\(x = 1\\)."}))

    assert formula in text, text
    assert "𝑥 = 1" in text, text


@pytest.mark.network
def test_gather_star_in_formula_text_keeps_its_star(tmp_path):
    text = text_of(render(tmp_path, {"q1": "Name: $\\text{gather*}$."}))

    assert "gather*" in text, text


@pytest.mark.network
def test_formula_shown_as_source_keeps_gather_star(tmp_path):
    formula = "\\iftypst \\begin{gather*} a \\end{gather*}"

    text = text_of(render(tmp_path, {"q1": f"Code: ${formula}$."}))

    assert formula in text, text


@pytest.mark.network
def test_failing_environment_is_shown_as_its_source(tmp_path):
    bad = "\\begin{align}\n\\foobar{a} &= b \\\\\nc &= d\n\\end{align}"

    text = text_of(render(tmp_path, {"q1": f"Before.\n\n{bad}\n\nAfter."}))

    assert " ".join(bad.split()) in text
    assert "$$" not in text
    assert "shown as LaTeX source" in text


@pytest.mark.network
def test_missing_answer_cell_is_noted(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "First."})
    out = tmp_path / "out.pdf"

    render_manual_pdf(notebook, ["q1", "q2"], {}, out)
    reader = PdfReader(out)

    assert len(reader.pages) == 2
    assert "No answer cell answer_q2 found" in text_of(reader)


@pytest.mark.parametrize(
    "answer", ["*Write your answer here.*", "  *Write your\n answer here.*", " "]
)
@pytest.mark.network
def test_untouched_or_blank_answer_says_no_answer(tmp_path, answer):
    placeholders = {"answer_q1": "*Write your answer here.*"}

    text = text_of(render(tmp_path, {"q1": answer}, placeholders=placeholders))

    assert "No answer." in text
    assert "Write your answer here" not in text


@pytest.mark.network
def test_untouched_part_says_no_answer_next_to_an_answered_one(tmp_path):
    notebook = tmp_path / "hw.py"
    notebook.write_text(
        student_copy().replace(
            "def answer_q3():",
            'def answer_q3_2():\n    mo.md("*Part (b).*")\n    return\n\n\n'
            "@app.cell\ndef answer_q3():",
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.pdf"
    placeholders = {"answer_q3": "Something else.", "answer_q3_2": "*Part (b).*"}

    render_manual_pdf(notebook, ["q3"], {}, out, placeholders=placeholders)
    text = text_of(PdfReader(out))

    assert ANSWER in text
    assert "No answer." in text
    assert "Part (b)" not in text


@pytest.mark.parametrize(
    "bad",
    [
        r"$\foobar{x} + \qux$",
        r"$$\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}$$",
    ],
)
@pytest.mark.network
def test_failing_formula_is_shown_as_source(tmp_path, bad):
    reader = render(tmp_path, {"q1": f"Good: $\\alpha + \\beta$. Bad: {bad} End of answer."})
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert "End of answer" in text
    assert bad.strip("$").split("{")[0] in text
    assert "𝛼" in text and "\\alpha" not in text
    assert "shown as LaTeX source" in text


BROKEN_HTML = '<table><tr><td rowspan="x">cell</td></tr></table> and $\\alpha$.'


@pytest.mark.network
def test_answer_that_defeats_the_math_fallback_is_plain_text(tmp_path):
    reader = render(tmp_path, {"q1": BROKEN_HTML, "q2": "Fine."})
    text = text_of(reader)

    assert len(reader.pages) == 2
    assert BROKEN_HTML in text
    assert "shown as plain text" in text
    assert "Fine." in text


@pytest.mark.network
def test_anchor_links_show_their_text(tmp_path):
    text = text_of(render(tmp_path, {"q1": "See [the proof](#nowhere) and $\\alpha$."}))

    assert "See the proof and 𝛼." in text
    assert "could not be rendered" not in text


@pytest.mark.network
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


@pytest.mark.network
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
@pytest.mark.network
def test_typst_hidden_in_latex_is_shown_not_run(tmp_path, formula):
    reader = render(tmp_path, {"q1": f"Before {formula} after $\\hspace{{1em}} \\alpha$."})
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert formula.strip("$") in text
    assert "𝛼" in text and "hspace{1em}" not in text
    assert "could not be rendered" not in text


@pytest.mark.network
def test_text_commands_in_math_render(tmp_path):
    answer = (
        r"Bold $\mathbf{v} = \textbf{v}$, italic $\textit{w}$, red $\textcolor{red}{x+1}$, "
        r"$\colorbox{yellow}{y}$, $f(x) = 1 \text{ if $x>0$}$, ${\color{blue} z}$, $\emph{e}$."
    )

    text = text_of(render(tmp_path, {"q1": answer}))

    assert "\\" not in text, text
    assert "𝐯" in text


@pytest.mark.network
def test_hash_that_mitex_escapes_is_text(tmp_path):
    reader = render(tmp_path, {"q1": r"$\text{x}#pagebreak()$"})

    assert len(reader.pages) == 1
    assert "#𝑝𝑎𝑔𝑒𝑏𝑟𝑒𝑎𝑘()" in text_of(reader)


@pytest.mark.network
def test_typst_in_an_answer_is_shown_not_run(tmp_path):
    answer = "#pagebreak() #set page(width: 1cm) $#pagebreak()$\n\n<!--raw-typst #pagebreak()-->"

    reader = render(
        tmp_path, {"q1": answer}, titles={"q1": '#pagebreak() *Q* $x$ "'}, prompts={"q1": "#a"}
    )
    text = text_of(reader)

    assert len(reader.pages) == 1
    assert '#pagebreak() *Q* $x$ "' in text
    assert "#pagebreak() #set page(width: 1cm)" in text


@pytest.mark.network
def test_images_show_their_alt_text(tmp_path):
    answer = 'A ![plot of the loss](loss.png) and <img src="x.png"> and ![](y.png).'

    text = text_of(render(tmp_path, {"q1": answer}))

    assert "[Image: plot of the loss]" in text
    assert text.count("[Image: no description]") == 2


@pytest.mark.network
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


@pytest.mark.network
def test_status_is_reported_before_the_questions_are_built(tmp_path, monkeypatch):
    events = []
    real = _answers.manual_question

    def built(*args, **kwargs):
        events.append("built")
        return real(*args, **kwargs)

    monkeypatch.setattr(_answers, "manual_question", built)

    render(tmp_path, {"q1": "x", "q2": "y"}, status=events.append)

    assert events == ["Creating the PDF of your written answers…", "built", "built"]


# Failures


def test_unreadable_notebook_gives_pdf_unavailable(tmp_path):
    with pytest.raises(PdfUnavailable, match="cannot be read"):
        render_manual_pdf(tmp_path / "missing.py", ["q1"], {}, tmp_path / "out.pdf")

    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")

    with pytest.raises(PdfUnavailable, match="cannot be read"):
        render_manual_pdf(broken, ["q1"], {}, tmp_path / "out.pdf")


def test_unwritable_output_gives_pdf_unavailable(tmp_path, monkeypatch):
    notebook = write_notebook(tmp_path, {"q1": "x"})
    # Only the write is under test: no Typst run, so no package download.
    monkeypatch.setattr(_native, "render_pdf", lambda questions: b"%PDF-stub")

    with pytest.raises(PdfUnavailable, match=r"^writing the PDF failed \(FileNotFoundError: .+\)$"):
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "no" / "such" / "out.pdf")


def test_a_document_that_never_compiles_gives_pdf_unavailable(tmp_path, monkeypatch):
    notebook = write_notebook(tmp_path, {"q1": "x"})

    def never(questions):
        raise _native.TypstError("unknown variable: boom")

    monkeypatch.setattr(_native, "render_pdf", never)

    with pytest.raises(PdfUnavailable) as raised:
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "out.pdf")

    assert str(raised.value) == "Typst failed (TypstError: unknown variable: boom)"
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.usefixtures("offline_typst")
def test_failed_package_download_gives_pdf_unavailable(tmp_path):
    notebook = write_notebook(tmp_path, {"q1": "x"})

    with pytest.raises(PdfUnavailable) as raised:
        render_manual_pdf(notebook, ["q1"], {}, tmp_path / "out.pdf")

    message = str(raised.value)
    assert message.startswith("downloading the Typst package @preview/cmarker:0.1.10 failed (")
    assert message.endswith(")")
    assert "\n" not in message
    assert not (tmp_path / "out.pdf").exists()


def test_download_error_is_a_plain_exception():
    assert issubclass(_native.DownloadError, Exception)
    assert str(_native.DownloadError("boom")) == "boom"


def test_typst_error_is_a_plain_exception():
    assert issubclass(_native.TypstError, Exception)
    assert str(_native.TypstError("boom")) == "boom"


def test_unknown_role_names_the_value():
    bogus = Part("x", "bogus", Style.MARKDOWN)  # ty: ignore[invalid-argument-type]  # on purpose

    with pytest.raises(TypeError) as raised:
        _native.render_pdf([Question("Q", (bogus,))])

    chain = []
    error: BaseException | None = raised.value
    while error is not None:
        chain.append(f"{type(error).__name__}: {error}")
        error = error.__cause__

    assert chain == [
        "TypeError: failed to extract field Question.parts",
        "TypeError: failed to extract field Part.role",
        "ValueError: unknown role 'bogus'",
    ]


@pytest.mark.network
def test_render_pdf_gives_the_pdf_bytes():
    question = Question("Title", (Part("Hello $x$.", Role.ANSWER),))

    data = _native.render_pdf([question])

    assert data.startswith(b"%PDF")
    assert "Title Hello 𝑥." in text_of(PdfReader(BytesIO(data)))


@pytest.mark.network
def test_interrupt_stops_the_pdf(tmp_path):
    # Every formula fails, so each part costs several Typst runs: over ten seconds in all. The
    # run's own tag keeps Typst's caches of earlier runs from helping.
    tag = uuid.uuid4().hex
    answers = {f"q{at}": f"Bad $\\foo{tag}$, $\\bar{at}$ and $\\baz{at}$." for at in range(400)}
    notebook = write_notebook(tmp_path, answers)
    timer = threading.Timer(0.5, _thread.interrupt_main)
    started = []

    def status(message):
        # Called just before the questions are built and Typst starts.
        started.append(time.monotonic())
        timer.start()

    try:
        with pytest.raises(KeyboardInterrupt):
            render_manual_pdf(notebook, list(answers), {}, tmp_path / "out.pdf", status=status)
    finally:
        timer.cancel()

    assert started
    assert time.monotonic() - started[0] < 2
    assert not (tmp_path / "out.pdf").exists()


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


@pytest.mark.network
def test_latin_answers_use_only_the_embedded_fonts(tmp_path):
    answer = "Text with `code` and $x^2 + \\alpha$.\n\n```\nprint(1)\n```"

    names = font_names(render(tmp_path, {"q1": answer}))

    assert names
    assert all(
        any(font in name for font in ("LibertinusSerif", "NewCMMath", "DejaVuSansMono"))
        for name in names
    ), names


@pytest.mark.network
def test_cjk_and_emoji_do_not_break_the_pdf(tmp_path):
    reader = render(tmp_path, {"q1": "中文答案 🙂 and Latin."})

    assert "and Latin." in text_of(reader)
