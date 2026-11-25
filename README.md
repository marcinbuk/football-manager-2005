# Football Manager 2005 w przeglądarce (GitHub Codespaces — 0 zł, bez karty)

Gra **Football Manager 2005** uruchomiona w chmurze (Wine + Xvfb + noVNC + Nginx) na porcie `7860`:

- `/` — wybór własnego nicku (lub szybkie wejście jako Gość) i odizolowane okno gry dla każdego gracza
- `/saves?profile=<nick>` — prywatny menedżer zapisów gry (pobieranie, wgrywanie `.fm`, usuwanie i restart gry)

Wszystkie zapisy są przechowywane w trwałym katalogu `saves/<nick>/` na dysku Codespace i nie resetują się między uruchomieniami.
