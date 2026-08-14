import { Element, ElementContent, Root } from "hast"
import { toString } from "hast-util-to-string"
import { PluggableList } from "unified"
import { QuartzTransformerPlugin } from "../types"

/**
 * nLab-Chinese: bilingual page ergonomics.
 *
 * 1. Wraps everything after the "英文原文 / Original English" heading in a
 *    collapsed <details lang="en">, so the Chinese translation leads and the
 *    English snapshot is one click away.
 * 2. Records the Chinese-only text as file.data.searchExcerpt so the search
 *    index does not double in size from the English snapshot.
 */
export const EnglishSection: QuartzTransformerPlugin = () => {
  return {
    name: "EnglishSection",
    htmlPlugins() {
      return [
        () => {
          return async (tree: Root, file) => {
            const children = tree.children
            let headingIdx = -1
            for (let i = 0; i < children.length; i++) {
              const node = children[i]
              if (
                node.type === "element" &&
                (node.tagName === "h2" || node.tagName === "h3") &&
                typeof toString(node) === "string" &&
                (toString(node).includes("英文原文") || toString(node).includes("Original English"))
              ) {
                headingIdx = i
                break
              }
            }
            if (headingIdx === -1) {
              return
            }

            // Chinese-only excerpt for the search index (before wrapping).
            const chineseTree: Root = {
              type: "root",
              children: children.slice(0, headingIdx),
            }
            file.data.searchExcerpt = toString(chineseTree)
            if (!file.data.text) {
              file.data.text = file.data.searchExcerpt
            }

            const details: Element = {
              type: "element",
              tagName: "details",
              properties: { className: ["english-original"], lang: "en", open: false },
              children: [
                {
                  type: "element",
                  tagName: "summary",
                  properties: {},
                  children: [
                    {
                      type: "text",
                      value: "英文原文 / Original English（点击展开，机器翻译来源见上）",
                    },
                  ],
                },
                ...(children.slice(headingIdx + 1) as ElementContent[]),
              ],
            }
            tree.children = [...children.slice(0, headingIdx), details]
          }
        },
      ] as PluggableList
    },
  }
}

declare module "vfile" {
  interface DataMap {
    searchExcerpt: string
  }
}
