# Built from the repository root: docker build -f examples/langgraph-support/Dockerfile .
# Once flotta-langgraph is on PyPI, your own agent needs only:
#   FROM python:3.12-slim
#   RUN pip install flotta-langgraph[openai]
#   COPY agent.py .
#   CMD ["python", "agent.py"]
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv
WORKDIR /app
COPY adapters/ ./adapters/
COPY examples/langgraph-support/ ./examples/langgraph-support/
RUN cd examples/langgraph-support && uv sync --frozen --no-dev
CMD ["/app/examples/langgraph-support/.venv/bin/python", "/app/examples/langgraph-support/agent.py"]
