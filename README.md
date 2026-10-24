# assets 分支

这是一个孤立分支（与 main 没有共同历史），专门存放 README、文档和 PR 里用到的截图等二进制文件；main 上不放二进制文件。

- 文件只增不删、也不覆盖替换：旧版本的 README 引用的图片要一直能显示。要换图就加一个新文件。
- 新文件命名：`<name>-<yyyymmdd>.ext`，例如 `img/live-20261024.png`（现有 8 个文件保留原名）。
- 引用方式：`https://raw.githubusercontent.com/suyiiyii/alldayrec/assets/img/<file>`
