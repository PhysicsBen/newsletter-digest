"""Analyze email body structure from different newsletter sources."""
from src.db.session import init_db, get_session
from src.db.models import Newsletter, NewsletterSource
from bs4 import BeautifulSoup

init_db()

target_sources = [
    "hello@mindstream.news",
    "dan@tldrnewsletter.com",
    "thebatch@deeplearning.ai",
    "theaireport@mail.beehiiv.com",
    "lastweekinai+news@substack.com",
    "lon@dataelixir.com",
    "hello@langchain.dev",
    "news@llamaindex.ai",
    "datascienceweekly@substack.com",
    "thisweekinaiclub@substack.com",
]

with get_session() as s:
    for email in target_sources:
        src = s.query(NewsletterSource).filter_by(sender_email=email).first()
        if not src:
            print(f"--- {email}: NOT IN DB ---")
            continue
        nl = s.query(Newsletter).filter_by(source_id=src.id).first()
        if not nl or not nl.body_raw:
            print(f"--- {email}: NO BODY ---")
            continue

        soup = BeautifulSoup(nl.body_raw, "lxml")
        links = soup.find_all("a", href=True)
        http_links = [a for a in links if a["href"].startswith("http")]

        print(f"=== {src.display_name} ({email}) ===")
        print(f"Subject: {nl.subject[:80]}")
        is_html = nl.body_raw.strip().startswith("<")
        print(f"Body type: {'HTML' if is_html else 'plain text'}")
        print(f"Total links: {len(links)}, http links: {len(http_links)}")
        print()

        for a in http_links[:6]:
            href = a["href"]
            link_text = a.get_text(strip=True)

            # Walk up to find meaningful blurb context
            context_blocks = []
            node = a
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                text = node.get_text(separator=" ", strip=True)
                if len(text) > len(link_text) + 10:
                    context_blocks.append(text[:300])
                    break

            print(f"  HREF: {href[:80]}")
            print(f"  Link text: {link_text[:120]}")
            print(f"  Context: {context_blocks[0][:250] if context_blocks else '(none)'}")
            print()
        print("---\n")
