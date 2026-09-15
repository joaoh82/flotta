import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * An agent's words, rendered as the Markdown they were written in (FLOTTA-63).
 *
 * Agents answer in Markdown nearly every turn — tables, code, lists — and a
 * plain-text pane showed a `df -h` summary as rows of pipes.
 *
 * **What an agent writes is untrusted.** It reads repositories, web pages and
 * command output, and any of those can put text in its reply. So three things
 * Markdown would normally do are refused here, each for its own reason:
 *
 * - **Raw HTML is not rendered.** `react-markdown` drops it unless a raw-HTML
 *   plugin is added, and none is. Its text is shown, never its tags.
 * - **Images are not fetched.** A remote image is the webview fetching a URL
 *   of an agent's choosing, which breaks the rule the whole desktop app rests
 *   on: no URL is fetched from the frontend. The CSP (`img-src 'self' data:`)
 *   would block it anyway, as a broken icon; the alt text says more.
 * - **Links do not navigate.** The window has no opener capability, on
 *   purpose, so a clicked `<a href>` would replace the app itself with the
 *   page. A link is shown as its text and address, which a person can copy.
 */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-2 break-words text-neutral-900">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

const components: Components = {
  a: ({ href, children }) => {
    const label = textOf(children);
    return (
      <span className="underline decoration-neutral-300 decoration-dotted underline-offset-2">
        {children}
        {/* A bare URL autolinks to itself; saying it twice adds nothing. */}
        {href && href !== label && (
          <span className="ml-1 font-mono text-[11px] text-neutral-500">({href})</span>
        )}
      </span>
    );
  },
  img: ({ alt }) => (
    <span className="italic text-neutral-500">[image{alt ? `: ${alt}` : ""}]</span>
  ),
  p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
  h1: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="font-semibold">{children}</h4>,
  h4: ({ children }) => <h4 className="font-semibold">{children}</h4>,
  ul: ({ children }) => <ul className="list-disc space-y-0.5 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-0.5 pl-5">{children}</ol>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-neutral-300 pl-3 text-neutral-600">
      {children}
    </blockquote>
  ),
  // Inline code; `pre` below resets it inside a block.
  code: ({ children }) => (
    <code className="rounded bg-neutral-100 px-1 py-0.5 font-mono text-[12px]">{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded bg-neutral-100 p-3 font-mono text-[12px] leading-relaxed [&>code]:bg-transparent [&>code]:p-0">
      {children}
    </pre>
  ),
  // Wide tables scroll inside their own box rather than widening the pane.
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-neutral-200 bg-neutral-50 px-2 py-1 text-left font-medium">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border border-neutral-200 px-2 py-1">{children}</td>,
  hr: () => <hr className="border-neutral-200" />,
};

/** The plain text inside rendered children, for comparing a link to its address. */
function textOf(node: React.ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return "";
}
