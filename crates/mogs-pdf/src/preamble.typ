
#import "@preview/cmarker:0.1.10"
#import "@preview/mitex:0.2.7": mitex, mitex-convert

#let aliases = "#(textmath|textbf|textit|textrm|texttt|textsf|textup|textmd|textnormal|emph|colortext|mitexcolor|mitexcolorbox|math\\.equation)\\b"
#let spacing = "\\\\(hspace|vspace|raisebox)\\*?"
#let length = regex("^\\s*-?(\\d+(\\.\\d*)?|\\.\\d+)\\s*(pt|mm|cm|in|em)\\s*$")

#let runs-code(src) = {
  if src.contains("iftypst") { return true }

  let commands = src.matches(regex(spacing))
  let lengths = src
    .matches(regex(spacing + "\\s*\\{([^{}]*)\\}"))
    .filter(m => m.captures.at(1).match(length) != none)
  if commands.len() != lengths.len() { return true }

  mitex-convert(src).replace(regex("\\\\."), "").replace(regex(aliases), "").contains("#")
}

#let safe-math(src, block: false) = {
  // mitex lacks `gather*`; its `gather` is unnumbered anyway. `raw` shows the original.
  let math = src
    .replace(regex("\\\\(begin|end)\\{gather\\*\\}"), m => "\\" + m.captures.at(0) + "{gather}")
  if runs-code(math) { raw(src, block: block) } else { mitex(math, block: block) }
}
