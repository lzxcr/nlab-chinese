import rehypeSanitize, { defaultSchema } from "rehype-sanitize"
import { PluggableList } from "unified"
import { QuartzTransformerPlugin } from "../types"

/**
 * nLab-Chinese: hard HTML whitelist applied after rehype-raw.
 *
 * Upstream nLab snapshots (and in the worst case, model output) are treated as
 * untrusted HTML.  Everything outside this whitelist is dropped, so no
 * <script>/<iframe>/event-handler markup can ever reach the rendered site.
 * This is defence in depth on top of the pipeline's markdown protector, which
 * already keeps raw HTML out of the model entirely.
 */
const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    // our own bilingual-page wrapper
    "details",
    "summary",
    // common nLab/Quartz structures
    "div",
    "span",
    "article",
    "section",
    "small",
    "mark",
    "kbd",
    "samp",
    "var",
    "figure",
    "figcaption",
    "ruby",
    "rt",
    "rp",
    "wbr",
    "datalist",
    "output",
    // math (KaTeX HTML output uses spans; MathML for a11y)
    "math",
    "maction",
    "maligngroup",
    "malignmark",
    "menclose",
    "merror",
    "mfenced",
    "mfrac",
    "mglyph",
    "mi",
    "mlabeledtr",
    "mlongdiv",
    "mmultiscripts",
    "mn",
    "mo",
    "mover",
    "mpadded",
    "mphantom",
    "mroot",
    "mrow",
    "ms",
    "mscarries",
    "mscarry",
    "msgroup",
    "msline",
    "mspace",
    "msqrt",
    "msrow",
    "mstack",
    "mstyle",
    "msub",
    "msup",
    "msubsup",
    "mtable",
    "mtd",
    "mtext",
    "mtr",
    "munder",
    "munderover",
    "semantics",
    "annotation",
    "annotation-xml",
  ],
  attributes: {
    ...defaultSchema.attributes,
    "*": [
      ...(defaultSchema.attributes?.["*"] ?? []),
      "className",
      "id",
      "lang",
      "dir",
      "title",
      "tabIndex",
      "open",
      "hidden",
      "ariaHidden",
      "ariaLabel",
      "dataSlug",
      "dataUrl",
    ],
    a: [...(defaultSchema.attributes?.a ?? []), "rel", "target", "dataSlug"],
    span: [
      ...(defaultSchema.attributes?.span ?? []),
      // KaTeX strut sizing is inline-styled; allow style only on spans.
      "style",
    ],
    img: [...(defaultSchema.attributes?.img ?? []), "loading", "width", "height"],
  },
}

export const Sanitize: QuartzTransformerPlugin = () => {
  return {
    name: "Sanitize",
    htmlPlugins() {
      return [[rehypeSanitize, schema]] as PluggableList
    },
  }
}
