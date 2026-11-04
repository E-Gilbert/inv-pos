"""Source of truth for CFC's menu as transcribed from "CFC_Menu_Food_and_Drinks.pdf" (22 pages, Oct 2026).

Run it to (re)generate menus/cfc_menu.csv, which scripts/load_menu.py and Setup → Import load into the POS:

    python scripts/build_cfc_menu_csv.py

Every price is exactly as printed, in KES, and INCLUDES VAT (menu footer: "Prices are quoted in Kenya
Shillings and inclusive of VAT"). Items marked active=0 are on the menu without a clear price; they stay
hidden on the POS until a manager confirms the price (see "flag").
Columns: category, major_group, name, variant, price, description, days, active, flag
  name + variant -> POS item "Name – Variant" grouped under one tile with a picker.
  days: weekdays the item can be sold (e.g. "Thu"); empty = every day.
"""
import csv
import os

F, B, X, S = "Food", "Beverage", "Extras", "Included sides"
ROWS = []


def add(cat, major, name, price, desc="", variant="", days="", active=1, flag=""):
    ROWS.append([cat, major, name, variant, price, desc, days, active, flag])


def variants(cat, major, name, pairs, desc="", flag=""):
    for v, p in pairs:
        add(cat, major, name, p, desc, v, flag=flag)


# ---------------------------------------------------------------- p2 Breakfast combos
c = "Breakfast"
add(c, F, "Grilled chicken breakfast", 650, "Grilled chicken breast, 2 eggs, 2 sausages, French fries, bread toast",
    )  # printed as "Grilled Chicken" on the breakfast page; renamed to avoid confusion with Grilled chicken (680)
add(c, F, "Pancake sampler", 600, "2 pancakes, 2 eggs (cooked to your liking), 2 sausages, baked beans, French fries")
add(c, F, "KK full chapati combo", 500, "2 chapatis, baked beans, beef stew, greens")
add(c, F, "English breakfast", 800, "2 eggs (cooked to your liking), 2 sausages, baked beans, liver, beverage of choice, cereal, toast bread")
add(c, F, "CFC breakfast combo", 550, "2 eggs, fresh fruit cut, samosa, nduma slices, 2 sausages")
add(c, F, "Omelette with toast", 300, "Toasted bread and an omelette garnished to your liking")
add(c, F, "Omelette combo", 500, "French omelette with a side of French fries")
add(c, F, "Loaded chapati combo", 550, "Chapati, 2 sausages, 2 eggs, French fries, baked beans, greens of choice")
add(c, F, "French omelette", 480)
add(c, F, "Plain omelette", 300)
add(c, F, "Spanish omelette", 420)
add(c, F, "Chapati rolex", 300)
add(c, F, "Esbesso chapati", 700)  # spelling as printed
add(c, F, "Liver chapati", 580)

# ---------------------------------------------------------------- p3 Quick bites
c = "Quick bites"
add(c, F, "Chicken fingers", 580)
add(c, F, "Regular chips", 250)
add(c, F, "Chapati", 60)
variants(c, F, "Shawarma", [("Beef", 500), ("Chicken", 500)])
add(c, F, "Doughnut", 60)
add(c, F, "Roast potatoes", 400)
add(c, F, "Naan", 80)
variants(c, F, "Sausages (2 pcs)", [("Beef", 150), ("Chicken", 150)])
add(c, F, "Potato wedges", 400)
add(c, F, "Chicken wings", 680)
variants(c, F, "Skewers", [("Beef", 580), ("Chicken", 580)])
add(c, F, "Garlic chips", 400)
variants(c, F, "Samosas (2 pcs)", [("Beef", 150), ("Chicken", 150), ("Veg", 150)])
for n, p in [("Maru bhajia", 400), ("Chapati saldato", 400), ("Bhajia", 400), ("Kebab", 100), ("Pancake", 150),
             ("Sweet potatoes", 150), ("Nduma", 150), ("Turbo naan", 80), ("Mandazi", 60), ("Chapati brown", 100),
             ("Mashed potatoes", 350), ("Peri-peri chips", 400), ("Chips masala", 400)]:
    add(c, F, n, p)

# ---------------------------------------------------------------- p4 Starters
c = "Salads"
add(c, F, "Greek salad", 350, "Crisp, evenly sliced cucumber, red onions, black olives")
variants(c, F, "Caesar salad", [("Beef", 450), ("Chicken", 450)], "Lettuce, croutons, topped with black olives")
add(c, F, "Coleslaw", 300, "Finely shredded cabbage and carrots in a creamy dressing")
add(c, F, "Fresh vegetable & egg salad", 300, "Cucumbers, tomatoes, red onions, greens, boiled eggs, choice of dressing")
c = "Soups"
soup = "Served with bread croutons or a bread slice"
add(c, F, "Vegetable soup", 300, "Fresh garden vegetables. " + soup)
add(c, F, "Alesso soup", 250, "Tender goat meat boiled in chef's special spices. " + soup)
add(c, F, "Mushroom soup", 400, "Mushrooms, herbs and olive oil. " + soup)
variants(c, F, "Chicken soup", [("Creamy", 350), ("Clear", 350)], soup)
add(c, F, "Carrot soup", 300, "Carrots, coconut milk and spices. " + soup)
add(c, F, "Spinach soup", 300, "Creamy spinach soup. " + soup)
add(c, F, "Cream of tomato", 300, "Steamed milk and tomatoes. " + soup)
add(c, F, "Ginger carrot soup", 350, "Cooking cream, carrots and ginger. " + soup)

# ---------------------------------------------------------------- p5 Special corner (served with rice or chapati)
c = "Vegetarian corner"
rc = "Served with rice or chapati"
for n, d in [("Peas & potato curry", "Potatoes and garden peas in curry sauce"), ("Vegetable curry", "Mixed vegetables in curry sauce"),
             ("Mushroom curry", "Mushrooms in curry sauce"), ("Potato mushroom curry", "Fried potatoes & mushrooms in curry sauce"),
             ("Paneer masala", "Smooth, creamy paneer"), ("Baingan bharta masala", "Vegetable baingan curry"),
             ("Vegetable pasta", "Fried pasta in pasta masala sauce")]:
    # Paneer masala is also printed at 680 on the Pasta & Rice page: owner's decision (2026-10-29) is the lower
    # price, 600, so it is sold once, from here.
    add(c, F, n, 600, d + ". " + rc)
c = "Indian corner"
add(c, F, "Chicken poussin", 950, "Chicken marinated in herbs and spices. " + rc)
add(c, F, "Butter chicken", 800, "Lightly charred chicken in a buttery, creamy tomato sauce. " + rc)  # also on Chicken meals page, same price
add(c, F, "BBQ chicken drumsticks", 900, "Crispy, juicy BBQ drumsticks. " + rc)
add(c, F, "Chicken malai tikka", 800, "Creamy marinated chicken skewers. " + rc)
add(c, F, "Chicken tikka boneless", 800, "Oven grilled boneless chicken. " + rc)  # also on Chicken meals page, same price
add(c, F, "Chicken Hyderabadi", 850, "Spicy, tangy chicken in Hyderabadi masala. " + rc)
add(c, F, "Tandoori fish tikka", 800, "Fish marinated in yoghurt & spices. " + rc)
add(c, F, "Fish tikka masala", 950, "Fish pieces in rich Indian spices. " + rc)

# ---------------------------------------------------------------- p6 Chicken meals
c = "Chicken meals"
acc = "Accompaniment of choice: fries, pilau, white rice, biryani rice, chapati, ugali (white/brown) or sautéed potatoes"
add(c, F, "Chicken masala", 780, "Chicken in rich, creamy tomato sauce with Indian spices")
add(c, F, "Chicken tikka", 850, "Marinated oven-baked whole chicken. " + acc)
add(c, F, "Chicken curry", 780, "Chicken simmered with spices, herbs, onions and tomatoes")
variants(c, F, "Chicken piecer", [("2 pieces", 590), ("3 pieces", 680)], "Breaded chicken pieces with fries and a side of salad")
add(c, F, "Chicken mushroom", 850, "Chicken simmered with spices, herbs, mushrooms, onions and tomatoes")
variants(c, F, "Sizzling chicken arosto", [("Full", 2200), ("Half", 1100)], "Oven-roasted then pan-fried with green pepper and red onions, on a hot plate")
add(c, F, "Chicken tikka masala", 800, "Boneless chicken in a subtly spiced tomato-cream sauce")
add(c, F, "Chicken Africana", 820, "Locally sourced chicken cooked to your taste")
variants(c, F, "Grilled chicken", [("Regular", 680), ("Full", 2200)],
         "Marinated grilled broiler chicken; regular with 1, full with 2 accompaniments of choice")

# ---------------------------------------------------------------- p7 Meat corner
c = "Meat corner"
macc = "Accompaniment of choice: fries, sautéed potatoes, biryani rice, pilau rice, ugali, chapati or white rice"
add(c, F, "Fried lamb", 800, "Crispy golden lamb pan-seared in red onions and bell pepper")
add(c, F, "Lamb chops", 900, "Grilled, pan-seared or baked")
add(c, F, "Dailo", 850, "Goat meat slow-cooked with Somali spices")
add(c, F, "Beef curry", 700, "Beef simmered with spices, herbs, onions and tomatoes")
variants(c, F, "Steak", [("Pepper steak", 800), ("Fillet steak", 800)], "Boneless beef tenderloin, best with fries/rice and kachumbari")
add(c, F, "Lamb curry", 800, "Goat meat simmered with spices, herbs, onions and tomatoes")
variants(c, F, "Sizzling goat arosto", [("1 kg", 1800), ("1/2 kg", 1000), ("Regular", 800)],
         "Oven-roasted goat pan-fried with green pepper and red onions, on a hot plate")
add(c, F, "Lamb mandi", 900, "Spice-rubbed goat shank oven-baked, goat stock rice, fried onions")
add(c, F, "Beef wet fry", 600, "Beef chunks in a rich tomato-based sauce")
add(c, F, "Mutton wet fry", 800, "Goat meat, wet fry or dry fry")
add(c, F, "Alesso", 800, "Goat meat boiled in chef's special Somali ingredients")

# ---------------------------------------------------------------- p8 Combos
c = "Combos"
add(c, F, "Kids platter", 1100, "Masala chips, chicken nuggets, sausage bites, shawarma")
add(c, F, "Regular combo", 1500, "1/4 chicken, mutton, beef, biryani rice, pilau, white rice, fries, side of salad")
add(c, F, "Family combo", 2600, "1/2 chicken, mutton, beef, biryani, pilau, white rice, chips, 2 chapatis, spaghetti, side of salad")
add(c, F, "Chips loaded combo", 3000, "Chicken drumsticks, chicken fingers, 1/2 chicken wet fried, deep fried wings, 3 portions of fries, 2 chapatis")
add(c, F, "Sharing feast", 6000, "Full chicken, 1 kg goat meat, potato wedges, fries, pilau, biryani, 4 fresh juices, 2 ice cream scoops, "
    "sausage bites, 2 shawarmas, chips masala")
add(c, F, "Chicken platter", 3100, "1/2 chicken, chicken fingers, honey glazed BBQ wings, drumsticks, biryani, pilau, white rice, fries")
add(c, F, "Singles combo", 950, "Chicken wings, nuggets, sausage bites, sautéed potatoes, fries")
add(c, F, "3-course meal combo", 3600, "1/2 chicken, nuggets, arosto, wings, pilau, biryani, white rice, fries, 2 bananas, 2 fresh juices, 2 ice cream scoops")
add(c, F, "Make your own combo", 0, "4 starches, 3 proteins, vegetables", active=0,
    flag="No price on the menu: hidden until a price is set.")

# ---------------------------------------------------------------- p9 Fish
c = "Fish"
add(c, F, "Fish munye", 900, "Grilled fish fillet in Swahili sauce, best with rice/fries; pairs with tartar sauce")
variants(c, F, "Victoria tilapia", [("Dry fry", 800), ("Wet fry", 800), ("Boiled", 800)], "Fresh from the lake, prepared to your liking")
add(c, F, "Fish fingers", 580, "Served with fries and tartar sauce")
add(c, F, "Fish curry", 800, "Fish simmered with rich spices, herbs, onions and tomatoes")
add(c, F, "Fish biryani", 800, "Basmati biryani with fish fillet")

# ---------------------------------------------------------------- p10 Pasta and rice
c = "Pasta"
for n, p in [("Macaroni salsa", 600), ("Pasta saldato", 600), ("Pasta arrabbiata", 600), ("Chilli paneer", 680),
             ("Pasta with minced meat", 650), ("Spaghetti bolognese", 600)]:
    add(c, F, n, p)
c = "Rice"
for n, p in [("Egg rice", 450), ("Coconut rice", 450), ("Vegetable rice", 380), ("Mandi rice", 600),
             ("Rice saldato", 600), ("Rice and sauce", 300)]:
    add(c, F, n, p)
variants(c, F, "Pilau", [("Chicken", 680), ("Beef", 680)])
variants(c, F, "Biryani", [("Chicken", 800), ("Beef", 800), ("Mutton", 800)])  # "Chicken biryani" (Chicken meals, 800) = Biryani – Chicken

# ---------------------------------------------------------------- p11 Burgers & sandwiches
c = "Burgers & sandwiches"
bs = "Served with fries and a side of salad"
for n, p in [("Chicken burger", 580), ("Beef burger", 580), ("Fish burger", 580), ("Double loaded burger", 750), ("Beef sandwich", 580)]:
    add(c, F, n, p, bs)

# ---------------------------------------------------------------- p12-13 Pizza
c = "Pizza"
pizzas = [("Peri-peri chicken pizza", "Grilled chicken, mozzarella, oregano, basil"),
          ("BBQ chicken pizza", "Grilled chicken in BBQ sauce, mozzarella, oregano, basil (best seller)"),
          ("Meat deluxe pizza", "Grilled beef, minced meat, chicken sausages, mozzarella, oregano, basil"),
          ("Chicken Hawaiian pizza", "Grilled chicken, pineapple, mozzarella, oregano, basil"),
          ("Chicken mushroom pizza", "Chicken, mushrooms, mozzarella, oregano, basil"),
          ("Veggie supreme pizza", "Mixed veg, green pepper, onions, mushrooms, pineapple, mozzarella (best seller)"),
          ("Cheese lovers pizza", "Mozzarella, oregano, basil (best seller)"),
          ("Hawaiian pizza", "Beef, pineapple, mozzarella, oregano, basil"),
          ("Margherita pizza", "Fresh tomatoes, mozzarella, oregano, basil")]
for n, d in pizzas:
    variants(c, F, n, [("Regular", 600), ("Medium", 800), ("Large", 1000), ("Mega", 1200)], d)
variants(c, F, "Thursday special", [("2 large pizzas", 1200), ("2 medium pizzas", 900)],
         "Epic Thursday: buy 1 get 1, medium & large. Note the two flavours on the order.")
for r in ROWS[-2:]:
    r[6] = "Thu"

# ---------------------------------------------------------------- p16 Beverages
c = "Coffee"
for n, s, d in [("Cappuccino", 200, 250), ("Americano", 200, 250), ("Espresso", 150, 200), ("Cafe latte", 200, 300),
                ("Mocha", 250, 350), ("Flat white", 180, 200), ("Black coffee", 180, 180), ("White coffee", 200, 300)]:
    # Black coffee is printed Single 200 / Double 180: owner's decision (2026-10-29) is the lower price, 180, for both.
    variants(c, B, n, [("Single", s), ("Double", d)])
for n, p in [("Vanilla latte", 350), ("Caramel latte", 350), ("Hazelnut latte", 350), ("Latte macchiato", 250), ("Espresso macchiato", 250)]:
    add(c, B, n, p)
c = "Tea & chocolate"
variants(c, B, "White chocolate", [("Single", 180), ("Double", 250)])
variants(c, B, "Black chocolate", [("Single", 150), ("Double", 200)])
for n, cup, pot in [("Mixed tea white", 150, 200), ("Black tea", 100, 150), ("Masala tea white", 180, 250), ("Masala tea black", 150, 200),
                    ("Lemon tea", 150, 200), ("Chai latte white", 150, 200), ("English tea", 200, 200), ("Ginger tea white", 180, 250),
                    ("Ginger tea black", 150, 180), ("Special tea (black/white)", 180, 300)]:
    # English tea: cup and pot are both printed at 200 and kept as printed (owner's decision 2026-10-29).
    variants(c, B, n, [("Cup", cup), ("Pot", pot)])
for n, p in [("Classic dawa", 250), ("Turmeric dawa", 300), ("Chai dawa", 300)]:
    add(c, B, n, p)
c = "Iced coffee"
add(c, B, "Black iced", 0, "Premium black tea brewed strong, served over ice", active=0,
    flag="Marked NEW on the menu but has no price: hidden until a price is set.")
add(c, B, "Iced americano", 300, "Bold espresso over ice with syrup")
add(c, B, "Iced latte", 250, "Smooth espresso with chilled milk")
add(c, B, "Mocha frappe", 350, "Coffee, chocolate and ice")
add(c, B, "Iced flat white", 300, "Velvety espresso with a light milk texture")
add(c, B, "Iced caramel", 300, "Iced treat")
add(c, B, "Iced vanilla latte", 300, "Iced treat")
add(c, B, "Iced mocha", 350, "Iced treat")

# ---------------------------------------------------------------- p18 Herbal aroma
c = "Herbal aroma"
for n, p, d in [("Chamomile", 250, "Soothing golden infusion"), ("Cinnamon", 300, "Sweet, rich & delicate"),
                ("Herbal lemon", 200, "Daily citrus ritual"), ("Hibiscus", 250, "Caffeine-free infusion"),
                ("Somali blend", 300, "Black or white tea with warming spices"), ("Herbal ginger tea", 200, "Fresh ginger root infusion")]:
    add(c, B, n, p, d)

# ---------------------------------------------------------------- p19 Signature drinks, p21 New
c = "Signature drinks"
for n, p, d in [("Berry mojito bliss", 450, "Lime, mint, strawberry, ice, splash of soda"),
                ("Strawberry colada", 450, "Real strawberries and coconut milk"),
                ("Tamarind breeze", 250, "Lemon juice, cinnamon, cardamom and tamarind"),
                ("Refresher mojito breeze", 450, "Lime juice, mint, ice, syrup, splash of soda"),
                ("Passion calpirasta", 450, "Lime, mint, passion, ice, splash of soda"),
                ("Pineapple dream", 450, "Fresh pineapple, no added sugar"),
                ("Berry Bliss (new)", 400, "Fresh strawberries and yogurt"),
                ("Blueberry colada", 450, ""), ("Blue lagoon mojito", 450, ""), ("Mango colada", 450, ""), ("Passion colada", 450, "")]:
    # Berry Bliss (new) is printed at 500 and the Berry bliss milkshake at 400: owner's decision (2026-10-29)
    # is the lower price, 400.
    add(c, B, n, p, d)
add(c, B, "Grape smoothie", 0, "Ripe grapes and milk", active=0,
    flag="On the 'New!' page without its own price (the 500 badge sits next to Berry Bliss): hidden until confirmed.")

# ---------------------------------------------------------------- p20 Drinks
c = "Shakes & smoothies"
variants(c, B, "Shake", [(v, 500) for v in ["Caffeine kick", "Banana bliss", "Sunshine shake", "Nutty delight", "Spearmint delight",
                                            "Choco shake", "Caramel dream", "Oreo shake"]])
variants(c, B, "Milkshake", [(v, 400) for v in ["Vanilla bliss", "Berry bliss", "Choco dream", "Fudge blast"]])
variants(c, B, "Smoothie", [(v, 450) for v in ["Tropical delight", "Mango smoothie", "Banana blend", "Strawberry smoothie"]])
add(c, B, "Pineapple mint", 300, "Special")
add(c, B, "Secko", 350, "Special")
c = "Juices & lemonades"
variants(c, B, "Lemonade", [(v, 350) for v in ["Sunshine fizz", "Berry sparkle", "Fresh splash"]])
variants(c, B, "Fresh juice", [(v, 250) for v in ["Mango", "Passion", "Pineapple", "Cocktail", "Melon"]], "Fresh delight")
variants(c, B, "Cocktail", [(v, 300) for v in ["Passion cocktail float", "Beetroot cocktail", "Mango beetroot cocktail"]])
c = "Fruits & ice cream"
add(c, F, "Full fruit salad", 400)
add(c, F, "Plain fruit salad", 280)
variants(c, F, "Ice cream", [("Single scoop", 100), ("Double scoop", 200), ("Triple scoop", 300)])
c = "Water & sodas"
for n, p in [("Sparkling water", 250), ("Soda", 100), ("Dasani 500ml", 100), ("Dasani 1L", 150), ("Keringet 500ml", 100), ("Keringet 1L", 150)]:
    add(c, B, n, p)

# ---------------------------------------------------------------- add-ons (priced extras, each a POS line)
c = "Add-ons"
for n, p in [("Add: Tartar sauce", 100), ("Add: Guacamole", 100), ("Add: Coleslaw", 100), ("Add: Piece of chicken", 400),
             ("Add: Banana", 30), ("Add: Mboga kienyeji", 100), ("Add: Extra salad", 100), ("Add: Coconut milk", 100),
             ("Add: Cheese slice", 100), ("Add: Vegetables", 100), ("Add: Beef (burger/sandwich)", 100),
             ("Add: Chicken (burger/sandwich)", 100), ("Add: Juice (burger/sandwich)", 250), ("Add: Soda (burger/sandwich)", 100),
             ("Add: Honey tot", 50), ("Add: Espresso shot", 150), ("Add: Tea leaves", 20), ("Add: Hot water", 50),
             ("Add: Ginger", 50)]:
    add(c, X, n, p)
variants(c, X, "Add: Sprinkle", [("Cinnamon", 100), ("Chocolate", 100)])
variants(c, X, "Add: Flavour", [("Vanilla", 100), ("Caramel", 100), ("Hazelnut", 100)])
variants(c, X, "Add: Raw spice", [("Turmeric", 100), ("Cardamom", 100), ("Chia seeds", 100)])

# ---------------------------------------------------------------- accompaniments included in the price (0.00 lines)
c = "Accompaniments (included)"
for v in ["Fries", "Pilau rice", "White rice", "Biryani rice", "Ugali (white)", "Ugali (brown)", "Chapati", "Sautéed potatoes",
          "Bread croutons", "Bread slice", "Kachumbari"]:
    add(c, S, "With", 0, "Included in the dish price: records the customer's choice for the kitchen and stock", v)


def main():
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "menus", "cfc_menu.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    names = set()
    for r in ROWS:
        full = f"{r[2]} – {r[3]}" if r[3] else r[2]
        if full.lower() in names:
            raise SystemExit(f"duplicate item {full}")
        names.add(full.lower())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "major_group", "name", "variant", "price", "description", "days", "active", "flag"])
        w.writerows(ROWS)
    print(f"{len(ROWS)} POS items written to {out}")


if __name__ == "__main__":
    main()
