import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"
import { normalizeBaseUrl } from "./quartz/util/siteUrl"

/**
 * Quartz 4 Configuration
 *
 * See https://quartz.jzhao.xyz/configuration for more information.
 *
 * Site URL: set NLAB_SITE_URL (e.g. https://nlab-chinese.example.org) before
 * deploying; it feeds sitemap/RSS/OG canonical URLs. The fallback is this
 * project's GitHub Pages host and keeps local builds deterministic.
 *
 * NLAB_OG_IMAGES=0 disables per-page OG image generation (recommended when the
 * translated corpus grows to tens of thousands of pages).
 */
const config: QuartzConfig = {
  configuration: {
    pageTitle: "nLab 中文",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    analytics: null,
    locale: "zh-CN",
    baseUrl: normalizeBaseUrl(process.env.NLAB_SITE_URL ?? "nlab-chinese.github.io"),
    ignorePatterns: ["private", "templates", ".obsidian"],
    defaultDateType: "modified",
    theme: {
      // System-installed CJK font stack: no external font CDN dependency.
      // Self-host a subset of Source Han Sans / Noto Sans SC under
      // quartz/static/fonts and add @font-face rules in custom.scss for fully
      // offline deployments.
      fontOrigin: "local",
      cdnCaching: false,
      typography: {
        header:
          '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Micro Hei"',
        body: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Micro Hei"',
        code: '"JetBrains Mono", "IBM Plex Mono", "Sarasa Mono SC", ui-monospace, SFMono-Regular, Menlo, monospace',
      },
      colors: {
        lightMode: {
          light: "#faf8f8",
          lightgray: "#e5e5e5",
          gray: "#b8b8b8",
          darkgray: "#4e4e4e",
          dark: "#2b2b2b",
          secondary: "#284b63",
          tertiary: "#84a59d",
          highlight: "rgba(143, 159, 169, 0.15)",
          textHighlight: "#fff23688",
        },
        darkMode: {
          light: "#161618",
          lightgray: "#393639",
          gray: "#646464",
          darkgray: "#d4d4d4",
          dark: "#ebebec",
          secondary: "#7b97aa",
          tertiary: "#84a59d",
          highlight: "rgba(143, 159, 169, 0.15)",
          textHighlight: "#b3aa0288",
        },
      },
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.CreatedModifiedDate({
        priority: ["frontmatter", "git", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: {
          light: "github-light",
          dark: "github-dark",
        },
        keepBackground: false,
      }),
      Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false }),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Latex({ renderEngine: "katex" }),
      // nLab-Chinese additions:
      // 1. hard HTML whitelist (defence against upstream/model HTML)
      Plugin.Sanitize(),
      // 2. collapsed lang="en" wrapper around the English original + search
      //    index that only carries the Chinese translation
      Plugin.EnglishSection(),
      Plugin.Description(),
    ],
    filters: [Plugin.RemoveDrafts()],
    emitters: [
      Plugin.AliasRedirects(),
      Plugin.ComponentResources(),
      Plugin.ContentPage(),
      Plugin.FolderPage(),
      Plugin.TagPage(),
      Plugin.ContentIndex({
        enableSiteMap: true,
        enableRSS: true,
      }),
      Plugin.Assets(),
      Plugin.Static(),
      Plugin.Favicon(),
      Plugin.NotFoundPage(),
      // Per-page OG images get expensive at scale (tens of thousands of
      // pages); disable with NLAB_OG_IMAGES=0.
      ...(process.env.NLAB_OG_IMAGES === "0" ? [] : [Plugin.CustomOgImages()]),
    ],
  },
}

export default config
