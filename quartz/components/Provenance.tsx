import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import style from "./styles/provenance.scss"

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

export default (() => {
  const Provenance: QuartzComponent = ({ fileData, displayClass }: QuartzComponentProps) => {
    const nlab = record(fileData.frontmatter?.nlab)
    if (!nlab) return null

    const translator = record(nlab.translator)
    const sourceUrl = text(nlab.source_url)
    const translatedAt = text(nlab.translated_at)
    const model = text(translator?.model)
    const promptVersion = text(translator?.prompt_version)
    const qualityStatus = text(nlab.quality_status)
    const qualityLabel = qualityStatus === "human-reviewed" ? "已人工校对" : "机器翻译 · 未人工校对"

    return (
      <aside class={`${displayClass ?? ""} provenance`} aria-label="页面来源与翻译信息">
        <span class="quality-badge">{qualityLabel}</span>
        {sourceUrl && (
          <a href={sourceUrl} rel="noopener noreferrer">
            查看 nLab 原文
          </a>
        )}
        {translatedAt && (
          <span>翻译时间：{translatedAt.replace("T", " ").replace("Z", " UTC")}</span>
        )}
        {model && <span>模型：{model}</span>}
        {promptVersion && <span>规则：{promptVersion}</span>}
      </aside>
    )
  }

  Provenance.css = style
  return Provenance
}) satisfies QuartzComponentConstructor
