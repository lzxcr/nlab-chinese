# 内容归属与来源

本文件说明仓库和生成站点中的第三方内容边界。站点内展示版本见
[content/ATTRIBUTION.md](content/ATTRIBUTION.md)。

## nLab 内容

页面原文来自 [nLab](https://ncatlab.org/nlab/show/HomePage) 及其贡献者。nLab 当前
说明：遵循通常学术惯例并注明来源时，使用和分发内容是自由且受鼓励的；社区尚未就
更正式的统一许可证声明形成共识。若相关个人贡献者另有说明，也应一并遵守。

每个通过发布门禁的译文页面记录：

- `nlab.page_id`、`nlab.source_url`
- `nlab.source_revision`、`nlab.source_content_md5`
- `nlab.fetched_at`、`nlab.translated_at`
- `nlab.translator.model`、`nlab.translator.prompt_version`
- `nlab.quality_status` 与内容使用说明

机器译文默认未经人工校对，不能替代英文原文或正式学术来源。

## 外部术语数据

项目曾使用香蕉空间（Banana Space）的 Translist 词条作为术语种子，但未找到允许
再分发该数据集的明确公开许可。因此仓库不提交该词典；运营者只能通过
`EXTERNAL_GLOSSARY_FILE` 在本地提供其有权使用的数据。

## 软件

项目代码采用 [MIT License](LICENSE.txt)。`quartz/` 基于 Jacky Zhao 的 Quartz v4，
保留其 MIT 版权声明和 [许可证副本](quartz/LICENSE.txt)。依赖包继续适用各自许可。

公开部署或再分发内容前，应重新核对 nLab 的最新使用说明、相关贡献者要求和任何本地
外部词典的许可。
