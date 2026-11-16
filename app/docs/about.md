# About and credits

## The creator

Root Cause Console is designed and built by **Aymane KENBOUCH**.

- **Email:** [aymanekenbouch@gmail.com](mailto:aymanekenbouch@gmail.com)
- **GitHub:** [github.com/AymaneK24](https://github.com/AymaneK24)

## How to reach me

Write to [aymanekenbouch@gmail.com](mailto:aymanekenbouch@gmail.com) for:

- **Contributing:** issues and pull requests on GitHub are welcome.
- **Bugs and ideas:** what you did, what you expected, what happened, and the console's version (`git log --oneline -1`). Remove host names, addresses, tokens, and personal data from logs and screenshots first.
- **Security problems in the software:** steps to reproduce, and please allow time for a fix before sharing details. See [Security](/docs/security#report-a-vulnerability).
- **Help with deploying it:** which setup you use (localhost, an Azure VM, or AKS), and the exact error.

> **Note:** For anything about your account or your data on a particular installation, such as access, a forgotten password, or a deletion request, contact that installation's administrator, who runs it and holds the data. The creator has no access to any installation.

## License

Root Cause Console is open source under the Apache License, Version 2.0. Copyright 2026 Aymane KENBOUCH. You may use, modify, and distribute it, including commercially, under the terms of that license. The `LICENSE` and `NOTICE` files in the project have the full text.

## Built with

| Component | Use | License |
|---|---|---|
| Python, FastAPI, Uvicorn, httpx, Pydantic | The web server and connections | PSF, MIT, BSD |
| PostgreSQL, psycopg | The database | PostgreSQL License, LGPL 3.0 |
| cryptography, argon2-cffi | Encryption and password hashing | Apache 2.0 or BSD, MIT |
| boto3 | Amazon CloudWatch | Apache 2.0 |
| ReportLab, Pillow | PDF reports and email charts | BSD, MIT-CMU |
| Caddy | HTTPS | Apache 2.0 |
| marked, DOMPurify | Showing answers and these docs safely | MIT, Apache 2.0 or MPL 2.0 |
| uPlot | Charts | MIT |
| Schibsted Grotesk, JetBrains Mono | Fonts | SIL Open Font License 1.1 |

The licenses of the files served to your browser are in `/vendor/THIRD-PARTY-LICENSES.txt` on every installation.
