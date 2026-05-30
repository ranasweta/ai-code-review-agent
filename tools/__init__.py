"""Tools package.

"Tools" are the real, deterministic capabilities the agents call — as opposed
to LLM reasoning. Mixing tools with LLMs is what makes this "tool-augmented".

- github_tool.py  Fetch PR diff, files, and metadata from the GitHub API.
- linter_tool.py  Run pylint / regex checks programmatically.
- ast_tool.py     Parse code structure using Python's `ast` module.

Implemented on Day 2.
"""
