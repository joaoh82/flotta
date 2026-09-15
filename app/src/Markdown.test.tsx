import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "./Markdown";

const render = (text: string) => renderToStaticMarkup(<Markdown text={text} />);

describe("Markdown", () => {
  it("renders the table eng-e actually sent as a table", () => {
    // Verbatim from the reply that showed as rows of pipes.
    const html = render(
      "Here's the disk usage:\n\n| Filesystem | Size | Used |\n|---|---|---|\n| none | 7.8G | 116M |\n| /dev/vdc | 974M | 50M |",
    );
    expect(html).toContain("<table");
    expect(html).toContain("<th");
    expect(html).toContain(">/dev/vdc</td>");
    expect(html).not.toContain("|---|");
  });

  it("renders code blocks, inline code and lists", () => {
    const html = render("Run `ls`:\n\n```sh\nuname -a\n```\n\n- one\n- two\n\n1. first");
    expect(html).toContain("<pre");
    expect(html).toContain("uname -a");
    expect(html).toContain("<code");
    expect(html).toMatch(/<ul[^>]*>\s*<li>one<\/li>/);
    expect(html).toContain("<ol");
  });

  it("never renders HTML an agent wrote", () => {
    // Agent output is untrusted: a repository it read can put anything here.
    const html = render(
      'Done. <img src="https://evil.example/x.png" onerror="alert(1)"><script>alert(1)</script><a href="https://evil.example">x</a>',
    );
    // No element is created from it...
    expect(html).not.toMatch(/<img|<script|<a[\s>]/i);
    // ...and it is not silently swallowed either: the person sees what the
    // agent wrote, as text.
    expect(html).toContain("&lt;script&gt;");
  });

  it("never fetches a Markdown image, and shows its alt text instead", () => {
    // A remote image would be the webview fetching a URL of an agent's choice.
    const html = render("![build status](https://evil.example/track.png)");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("evil.example");
    expect(html).toContain("[image: build status]");
  });

  it("shows a link as text and address, never as something to navigate", () => {
    // No opener capability: a clicked href would replace the app with the page.
    const html = render("See [the PR](https://github.com/joaoh82/flotta/pull/101).");
    expect(html).not.toMatch(/<a[\s>]/);
    expect(html).not.toContain("href");
    expect(html).toContain("the PR");
    expect(html).toContain("(https://github.com/joaoh82/flotta/pull/101)");
  });

  it("does not repeat a bare URL next to itself", () => {
    const html = render("Open https://flotta.dev now");
    expect(html).not.toMatch(/<a[\s>]/);
    expect(html.match(/https:\/\/flotta\.dev/g)).toHaveLength(1);
  });

  it("neutralises javascript links rather than printing them as addresses to follow", () => {
    const html = render("[click](javascript:alert(1))");
    expect(html).not.toMatch(/<a[\s>]/);
    expect(html).not.toContain("javascript:");
  });
});
