# Bric WordPress bridge

Ponte senza sessione browser e senza password condivise tra la routine
editoriale e WordPress.

## Garanzie operative

- usa l'utente WordPress tecnico `bric_automation` con ruolo **Autore**;
- accetta esclusivamente manifest con `status: pending`;
- forza nuovamente `status: pending` nella richiesta REST;
- evita duplicati verificando lo slug prima di creare l'articolo;
- usa soltanto la categoria WordPress già esistente;
- carica immagini JPEG, PNG o WebP e può impostare la cover;
- non dipende da cookie, Chrome, desktop remoto o password applicative;
- autentica ogni esecuzione con un token GitHub OIDC firmato e di breve durata.

## Uso dalla routine

La routine crea un file `inbox/YYYY-MM-DD-slug.json` conforme a
`schema/article.example.json` e gli asset sotto `inbox/assets/`. Il push su
`main` avvia GitHub Actions, verifica l'autenticazione REST e crea l'articolo
in **attesa di revisione**.

I metadati SEO sono conservati nel manifest per la revisione. Yoast non espone
la scrittura dei propri campi privati tramite la REST API standard: il ponte
non li falsifica e non modifica plugin o tema per aggirare questa limitazione.

## Autenticazione

Il workflow richiede `id-token: write`, ottiene un token OIDC con audience
`bric-wordpress-bridge` e lo presenta a WordPress. Lo snippet WordPress verifica
firma, scadenza e questi vincoli:

- repository e ID GitHub esatti;
- branch `main`;
- ambiente `production`;
- file workflow esatto;
- evento `push` o `workflow_dispatch`.

`WP_URL` è fissato nel workflow al solo sito live
`https://www.bricdellavigna.it` per impedire invii accidentali allo staging.

## Test manuale

Da **Actions → WordPress pending-review bridge → Run workflow**, scegliere
`connection-test`. Il test esegue soltanto una richiesta autenticata in
lettura e non crea articoli.
