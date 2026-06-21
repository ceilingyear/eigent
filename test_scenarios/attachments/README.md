# Eigent 文件处理与长任务测试附件

## Task 1 - 多格式资料汇总

目的：测试 PDF、DOCX、CSV、PNG 混合附件读取、跨文件汇总、冲突识别和报告生成能力。

附件目录：`task1_multiformat_research/`

上传文件：
- `market_brief_2026.pdf`
- `meeting_notes.docx`
- `finance_assumptions.csv`
- `workflow_snapshot.png`

Prompt 使用：`task1_multiformat_research/prompt.md`

## Task 2 - 脏 Excel 数据清洗

目的：测试 Excel 多 sheet 读取、数据清洗、异常识别、生成新 Excel 和分析报告能力。

附件目录：`task2_dirty_excel_cleanup/`

上传文件：
- `sales_ops_dirty_data.xlsx`

Prompt 使用：`task2_dirty_excel_cleanup/prompt.md`

## Task 3 - 代码压缩包 + 日志长任务

目的：测试 ZIP 解压、代码阅读、日志分析、代码修改、运行测试和修复报告生成能力。

附件目录：`task3_code_logs_long_task/`

上传文件：
- `sample_service.zip`
- `runtime.log`
- `requirements_change_request.md`

Prompt 使用：`task3_code_logs_long_task/prompt.md`
