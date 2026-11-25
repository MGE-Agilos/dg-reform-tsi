// Temporary password for one user of the explorer (AUTH_MODE=password), for the first administrator or when no
// administrator can sign in. Same settings as the service (environment variables of step 6):
//   node set-password.js name@example.eu              (Docker: docker exec -it <container> node set-password.js ...)
// The address must already be a user (table testers). The password is printed once; the user replaces it at the first
// sign-in. Administrators then give the passwords of the others from the Admin page.
import { loadConfig, ConfigError } from "./src/config.js";
import { store, giveTempPassword, normEmail, HttpError } from "./src/admin.js";

try {
  const cfg = loadConfig();
  const email = normEmail(process.argv[2]);
  const db = store(cfg, fetch);
  const row = await db.tester(email);
  if (!row) {
    console.error(`${email} is not a user of the explorer. Add it first, as the database owner:\n`
      + `  insert into testers (email, role) values ('${email}', 'admin');`);
    process.exit(1);
  }
  const password = await giveTempPassword(db, email);
  console.log(`Temporary password of ${email} (${row.role}): ${password}\n`
    + "Give it through a separate channel; it must be replaced at the first sign-in.");
} catch (e) {
  if (e instanceof ConfigError || e instanceof HttpError) {
    console.error(e.message);
    process.exit(2);
  }
  throw e;
}
