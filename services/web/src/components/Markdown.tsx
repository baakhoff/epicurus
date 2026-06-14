/** Assistant prose — GFM markdown, typeset serif (.ep-prose), no raw HTML. */
import { isValidElement, useCallback, useState, type ComponentPropsWithoutRef } from "react";
import { Check, Copy } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const LANG_RE = /language-(\w+)/;

function CodeBlock({ lang, text }: { lang: string | undefined; text: string }) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(() => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [text]);

  return (
    <div className="ep-code-block">
      <div className="ep-code-header">
        {lang && <span className="ep-code-lang">{lang}</span>}
        <button onClick={copy} aria-label="Copy code" className="ep-code-copy">
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
      </div>
      <pre>
        <code>{text}</code>
      </pre>
    </div>
  );
}

type PreProps = ComponentPropsWithoutRef<"pre"> & { node?: unknown };

function preRenderer({ children }: PreProps) {
  if (isValidElement(children) && children.type === "code") {
    const { className, children: text } = children.props as {
      className?: string;
      children?: unknown;
    };
    return (
      <CodeBlock
        lang={LANG_RE.exec(className ?? "")?.[1]}
        text={String(text ?? "").replace(/\n$/, "")}
      />
    );
  }
  return <pre>{children}</pre>;
}

/** Close any unclosed fenced code block so partial fences during streaming render as code. */
export function closeFence(md: string): string {
  let open = false;
  for (const line of md.split("\n")) {
    if (/^```/.test(line)) open = !open;
  }
  return open ? md + "\n```" : md;
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="ep-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{ pre: preRenderer }}
      >
        {closeFence(children)}
      </ReactMarkdown>
    </div>
  );
}
