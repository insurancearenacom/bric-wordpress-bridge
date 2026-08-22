# Bric WordPress bridge

Ponte senza sessione browser tra la routine editoriale e WordPress.

## Garanzie operative

- usa l'utente WordPress tecnico `bric_automation` con ruolo **Autore**;
- accetta esclusivamente manifest con `status: pending`;
- forza nuovamente `status: pending` nella richiesta REST;
- evita duplicati verificando lo slug prima di creare l'articolo;
- usa soltanto la categoria WordPress giÃ  esistente;
- carica immagini JPEG, PNG o WebP e puÃ² impostare la cover;
- non dipende da cookie, Chrome o desktop remoto.

## Uso dalla routine

La routine crea un file `inbox/YYYY-MM-DD-slug.json` conforme a
`schema/article.example.json` e gli asset sotto `inbox/assets/`. Il push su
`main` avvia GitHub Actions, verifica l'autenticazione REST e crea l'articolo
in **attesa di revisione**.

I metadati SEO sono conservati nel manifest per la revisione. Yoast non espone
la scrittura dei propri campi privati tramite la REST API standard: il ponte
non li falsifica e non modifica plugin o tema per aggirare questa limitazione.

## Secret GitHub

Nell'ambiente `production` devono essere presenti:

- `WP_USERNAME`
- `WP_APP_PASSWORD`

`WP_URL` Ã¨ fissato nel workflow al solo sito live
`https://www.bricdellavigna.it` per impedire invii accidentali allo staging.

## Test manuale

Da **Actions â†’ WordPress pending-review bridge â†’ Run workflow**, scegliere
`connection-test`. Il test esegue soltanto una richiesta autenticata in
lettura e non crea articoli.

