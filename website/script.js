// Aus der zerlegten E-Mail-Adresse einen anklickbaren Link machen. Das @ selbst
// steht nur im Stylesheet (siehe `.mail-at`), damit ein Adressensammler im
// Quelltext kein `etwas@etwas.de` findet. Ohne JavaScript bleibt die Adresse
// trotzdem vollständig lesbar — sie ist dann nur nicht klickbar.
for (const feld of document.querySelectorAll(".mail")) {
  const benutzer = feld.querySelector(".mail-benutzer");
  const anbieter = feld.querySelector(".mail-anbieter");
  if (!benutzer || !anbieter) continue;

  const adresse = `${benutzer.textContent.trim()}@${anbieter.textContent.trim()}`;
  const link = document.createElement("a");
  link.href = `mailto:${adresse}`;
  link.textContent = adresse;
  feld.replaceChildren(link);
}
