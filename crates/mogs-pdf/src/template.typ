#set page(paper: "us-letter", margin: 2cm)

// Typst's embedded fonts; system fonts only fill in glyphs these lack, such as CJK.
#set text(font: "Libertinus Serif", size: 11pt)
#show math.equation: set text(font: "New Computer Modern Math")
#show raw: set text(font: "DejaVu Sans Mono")

#let image-stub(source, alt: none, ..args) = {
  let label = if alt == none or alt == "" { "no description" } else { alt }
  emph("[Image: " + label + "]")
}

// A link to an anchor in the answer shows just its text: a missing label would stop Typst.
#let link-stub(dest, body) = if type(dest) == label { body } else { link(dest, body) }

#let markdown(text) = cmarker.render(
  text,
  math: safe-math,
  h1-level: 2,
  raw-typst: false,
  set-document-title: false,
  scope: (image: image-stub, link: link-stub),
)

#let show-part(part) = block(width: 100%, {
  if part.style == "markdown" {
    markdown(part.text)
  } else if part.style == "note" {
    text(fill: gray, emph(part.text))
  } else {
    raw(part.text, block: true)
  }
  if part.note != none { block(above: 0.8em, text(size: 8pt, fill: gray, part.note)) }
})

#for (index, question) in json(bytes(sys.inputs.doc)).enumerate() {
  if index > 0 { pagebreak() }
  heading(level: 1, question.title)

  let parts = question.parts
  for (at, part) in parts.enumerate() {
    show-part(part)

    // A rule between a prompt and the answer after it.
    let next = parts.at(at + 1, default: none)
    if part.role == "prompt" and next != none and next.role == "answer" {
      line(length: 100%, stroke: 0.5pt + gray)
    }
  }
}
