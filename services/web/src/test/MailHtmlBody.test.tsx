import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { MailHtmlBody } from "@/components/MailHtmlBody";
import type { MailAttachment } from "@/lib/contracts";

const INLINE: MailAttachment = {
  id: "att-logo",
  filename: "logo.png",
  mime_type: "image/png",
  size: 1000,
  content_id: "logo",
  inline: true,
};

const HTML =
  '<p onclick="steal()">Hi</p>' +
  '<img src="cid:logo">' +
  '<img src="https://tracker.example/pixel.gif">' +
  '<script>window.parent.document.cookie</script>' +
  '<a href="https://example.com/read">read</a>';

function renderBody(attachmentUrl = (m: string, a: string) => `/proxy/${m}/${a}`) {
  return render(
    <MailHtmlBody html={HTML} messageId="m1" attachments={[INLINE]} attachmentUrl={attachmentUrl} />,
  );
}

function srcdoc(): string {
  return screen.getByTitle("Email message").getAttribute("srcdoc") ?? "";
}

it("renders in a sandboxed iframe that never allows scripts", () => {
  renderBody();
  const frame = screen.getByTitle("Email message");
  const sandbox = frame.getAttribute("sandbox") ?? "";
  expect(sandbox).not.toContain("allow-scripts");
  expect(sandbox).toContain("allow-same-origin"); // for auto-size + cookied cid proxy
});

it("strips scripts and inline event handlers", () => {
  renderBody();
  const doc = srcdoc();
  expect(doc).not.toContain("<script");
  expect(doc).not.toContain("onclick");
});

it("rewrites inline cid: images to the module attachment proxy", () => {
  renderBody();
  expect(srcdoc()).toContain("/proxy/m1/att-logo");
  expect(srcdoc()).not.toContain("cid:logo"); // the cid: ref is gone, resolved to the proxy
});

it("blocks remote images by default and offers to load them", () => {
  renderBody();
  const doc = srcdoc();
  // The tracking pixel's src is stashed as data-remote-src, not a live src attribute.
  expect(doc).toContain('data-remote-src="https://tracker.example/pixel.gif"');
  expect(doc).not.toMatch(/\ssrc="https:\/\/tracker\.example/); // no live remote src
  expect(screen.getByRole("button", { name: /load images/i })).toBeInTheDocument();
});

it("restores remote images after the user loads them", () => {
  renderBody();
  fireEvent.click(screen.getByRole("button", { name: /load images/i }));
  const doc = srcdoc();
  expect(doc).toMatch(/\ssrc="https:\/\/tracker\.example/); // now a live remote src
  // The banner/button is gone once images are loaded.
  expect(screen.queryByRole("button", { name: /load images/i })).not.toBeInTheDocument();
});

it("does not render inline images when no attachment proxy is available", () => {
  render(<MailHtmlBody html={HTML} messageId="m1" attachments={[INLINE]} />);
  // Without an attachmentUrl the cid: ref is dropped (no broken cid: request), not proxied.
  expect(srcdoc()).not.toContain("cid:logo");
  expect(srcdoc()).not.toContain("/proxy/");
});
