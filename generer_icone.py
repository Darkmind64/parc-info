"""Icône ParcInfo : écran + engrenage + document + pastille de validation.

Dessinée en géométrie plutôt que redimensionnée depuis une image : aux petites
tailles, un trait mis à l'échelle finit sous le pixel et l'icône devient une
tache grise. Ici l'épaisseur du trait est épaissie pour les petits formats, de
façon qu'il reste lisible à 16 px.
"""
import math
from PIL import Image, ImageDraw

NOIR = (17, 17, 17, 255)
VIDE = (0, 0, 0, 0)
TRAIT = 26                 # épaisseur de référence, en unités de dessin


def dessiner(taille, trait=TRAIT, sur=4, lignes=True, pastille_pleine=False):
    """Rend l'icône en `taille` px de côté, fond transparent.

    `lignes` et `pastille_pleine` allègent le dessin pour les petits formats :
    à 16 px, les trois lignes du document se rejoignent en un pâté et la
    pastille évidée se referme. Un aplat et moins de détail restent lisibles.
    """
    def u(v):
        return int(round(v * sur))

    W, H = u(820), u(600)
    img = Image.new('RGBA', (W, H), VIDE)
    d = ImageDraw.Draw(img)
    t = max(1, u(trait))

    def effacer(forme):
        """Retire ce qui se trouve dessous : les éléments se chevauchent."""
        masque = Image.new('L', (W, H), 0)
        forme(ImageDraw.Draw(masque))
        img.paste(VIDE, (0, 0), masque)

    def rrect(dr, x0, y0, x1, y1, r, **kw):
        dr.rounded_rectangle([u(x0), u(y0), u(x1), u(y1)], radius=u(r), **kw)

    def bout(x, y):
        """Extrémité arrondie d'un trait."""
        d.ellipse([u(x) - t // 2, u(y) - t // 2, u(x) + t // 2, u(y) + t // 2],
                  fill=NOIR)

    def roue_dentee(cx, cy, r_dent, r_pied, dents):
        pts = []
        pas = 2 * math.pi / dents
        demi_dent, demi_pied = 0.26 * pas, 0.35 * pas
        for i in range(dents):
            a = i * pas - math.pi / 2
            for ang, r in ((a - demi_pied, r_pied), (a - demi_dent, r_dent),
                           (a + demi_dent, r_dent), (a + demi_pied, r_pied)):
                pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
            # Le creux entre deux dents suit un arc, pas une corde.
            for k in range(1, 4):
                ang = a + demi_pied + (pas - 2 * demi_pied) * k / 4
                pts.append((cx + r_pied * math.cos(ang), cy + r_pied * math.sin(ang)))
        return [(u(x), u(y)) for x, y in pts]

    # Écran, pied et socle
    rrect(d, 118, 20, 672, 400, 46, outline=NOIR, width=t)
    rrect(d, 338, 400, 452, 496, 10, fill=NOIR)
    rrect(d, 262, 484, 528, 532, 24, outline=NOIR, width=t)

    # Engrenage, à cheval sur le bord gauche
    cx, cy = 207, 250
    roue = roue_dentee(cx, cy, 150, 112, 10)
    effacer(lambda m: m.polygon(roue, fill=255))
    d.line(roue + [roue[0]], fill=NOIR, width=t, joint='curve')
    d.ellipse([u(cx - 60), u(cy - 60), u(cx + 60), u(cy + 60)],
              outline=NOIR, width=t)

    # Document, à cheval sur le bord droit
    effacer(lambda m: rrect(m, 428, 96, 700, 458, 42, fill=255))
    rrect(d, 428, 96, 700, 458, 42, outline=NOIR, width=t)
    if lignes:
        for x0, x1, y in ((480, 648, 172), (480, 648, 236), (480, 548, 300)):
            d.line([(u(x0), u(y)), (u(x1), u(y))], fill=NOIR, width=t)
            bout(x0, y)
            bout(x1, y)

    # Pastille de validation, sur l'angle du document
    px, py, pr = 636, 386, 96
    boite = [u(px - pr), u(py - pr), u(px + pr), u(py + pr)]
    effacer(lambda m: m.ellipse(boite, fill=255))
    coche = [(u(x), u(y)) for x, y in ((590, 388), (622, 422), (684, 350))]
    if pastille_pleine:
        d.ellipse(boite, fill=NOIR)
        d.line(coche, fill=(255, 255, 255, 255), width=int(t * 1.1),
               joint='curve')
    else:
        d.ellipse(boite, outline=NOIR, width=t)
        d.line(coche, fill=NOIR, width=t, joint='curve')
        for x, y in (coche[0], coche[-1]):
            d.ellipse([x - t // 2, y - t // 2, x + t // 2, y + t // 2], fill=NOIR)

    # Cadrage carré sur le contenu réellement dessiné, avec une marge : une
    # icône qui touche les bords est rognée par les arrondis de Windows.
    icone = img.crop(img.getbbox())
    cote = max(icone.size)
    marge = int(cote * 0.06)
    carre = Image.new('RGBA', (cote + 2 * marge,) * 2, VIDE)
    carre.paste(icone, (marge + (cote - icone.width) // 2,
                        marge + (cote - icone.height) // 2))
    return carre.resize((taille, taille), Image.LANCZOS)


#: Réglages par format. En dessous de 64 px, le trait de référence passe sous
#: le pixel : on l'épaissit, et on retire du détail plutôt que de le laisser
#: s'agglomérer. Valeurs retenues après comparaison des rendus au pixel.
FORMATS = {
    16: dict(trait=1.50, lignes=False, pastille_pleine=True),
    24: dict(trait=1.70, lignes=True,  pastille_pleine=True),
    32: dict(trait=1.50),
    48: dict(trait=1.25),
    64: dict(trait=1.10),
}


def rendre(taille):
    reglages = dict(FORMATS.get(taille, {}))
    facteur = reglages.pop('trait', 1.0)
    return dessiner(taille, trait=TRAIT * facteur, **reglages)


if __name__ == '__main__':
    import sys
    cible = sys.argv[1] if len(sys.argv) > 1 else '.'
    tailles = [16, 24, 32, 48, 64, 128, 256]
    images = [rendre(n) for n in tailles]
    maitre = rendre(1024)
    maitre.save(cible + '/icon_1024.png')
    rendre(256).save(cible + '/icon.png')
    # Pillow réécrit chaque plan à partir de la première image : on fournit
    # explicitement les variantes déjà dessinées à la bonne épaisseur.
    images[-1].save(cible + '/icon.ico', format='ICO',
                    sizes=[(n, n) for n in tailles],
                    append_images=images[:-1])
    print('écrit :', cible)
