# Madeleine at the Movies

Website for **Madeleine at the Movies**, a film review program on Golden Days Radio, hosted by Madeleine Swain.

Live at: [matm.com.au](https://matm.com.au)

---

## What's here

- `index.html` — the complete website (single file, no build step required)
- `CNAME` — tells GitHub Pages to serve the site at matm.com.au

---

## Deploying

The site is hosted on **GitHub Pages**. Any push to the `main` branch goes live automatically.

To enable GitHub Pages on a fresh fork:
1. Go to **Settings → Pages**
2. Set source to **Deploy from a branch**
3. Choose **main** / **(root)**
4. Save

---

## Pointing matm.com.au at GitHub Pages

In your GoDaddy DNS settings, add the following records:

| Type  | Name | Value                  |
|-------|------|------------------------|
| A     | @    | 185.199.108.153        |
| A     | @    | 185.199.109.153        |
| A     | @    | 185.199.110.153        |
| A     | @    | 185.199.111.153        |
| CNAME | www  | bl-air.github.io       |

DNS changes can take up to 24 hours to propagate. Once live, GitHub Pages will automatically provision an SSL certificate for the domain.

---

## Updating the site

All content is in `index.html`. To add a new review, copy one of the existing `.review-card` blocks and update the year, title, director, stars, and excerpt.

The portrait photo is `Madeleine-Swain.jpg` in the repo root, already wired into the About section.

---

*A Prahran Publishing production.*
