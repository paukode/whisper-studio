/**
 * Curated fact pack for the buddy companion. A grab bag of genuinely
 * interesting facts across science, biology, inventions, and geography.
 * Hand-picked and kept short (one sentence). This is the default source; the
 * opt-in "fresh facts (AI)" toggle swaps in /api/buddy/fact instead. Expand
 * freely, keeping each entry to one true, surprising sentence.
 */
export const BUDDY_FACTS: readonly string[] = [
  // Science: physics, chemistry, astronomy
  'Light takes about 8 minutes and 20 seconds to travel from the Sun to Earth.',
  'Lightning is roughly five times hotter than the surface of the Sun.',
  'Sound travels about four times faster through water than through air.',
  'Helium was discovered in the Sun’s spectrum in 1868, before it was ever found on Earth.',
  'Saturn is so light for its size that it would float in water, given a big enough bathtub.',
  'A day on Venus is longer than its entire year.',
  'Venus is the hottest planet in the solar system, even though Mercury sits closer to the Sun.',
  'Diamond and pencil graphite are both pure carbon, just with the atoms arranged differently.',
  'Glass is not a slow-flowing liquid; it is an amorphous solid, despite the old cathedral-window myth.',
  'A teaspoon of neutron-star material would weigh about a billion tonnes.',
  'The observable universe is roughly 93 billion light-years across.',
  'Under the right conditions hot water can freeze faster than cold, an effect named after Erasto Mpemba.',
  'Absolute zero, about minus 273.15 Celsius, is the lowest temperature physically possible.',
  // Biology: animals, the human body, plants
  'Octopuses have three hearts and blue, copper-based blood.',
  'Sharks are older than trees, having appeared roughly 100 million years earlier.',
  'Some jellyfish can revert to an earlier life stage, making them effectively biologically immortal.',
  'Tardigrades can survive the vacuum of space, extreme radiation, and being completely dried out.',
  'A hummingbird’s heart can beat more than 1,200 times a minute.',
  'A blue whale’s heart is about the size of a small car.',
  'Your body carries roughly as many bacterial cells as human ones.',
  'Bone is, weight for weight, several times stronger than steel.',
  'Bananas are botanically berries, while strawberries are not.',
  'Trees can trade nutrients and warnings through underground networks of fungi.',
  'Honey never spoils; edible honey has been found in 3,000-year-old Egyptian tombs.',
  'A snail can sleep for up to three years when conditions are too dry.',
  // Inventions and the history of technology
  'The microwave oven was discovered by accident when a radar engineer’s chocolate bar melted in his pocket.',
  'Bubble wrap was originally invented to be textured wallpaper.',
  'Velcro was inspired by the burrs that stuck to an inventor’s dog after a walk.',
  'Penicillin was found when Alexander Fleming left a petri dish uncovered and mould killed the bacteria.',
  'Ada Lovelace wrote what is considered the first computer algorithm in the 1840s, a century before electronic computers.',
  'The world’s first webcam watched a coffee pot so researchers could avoid trips to an empty one.',
  'Play-Doh was first sold as a cleaner for coal-smudged wallpaper.',
  'The Wright brothers’ first powered flight in 1903 lasted only about 12 seconds.',
  'Gutenberg’s printing press, around 1440, helped spark a wave of literacy across Europe.',
  // Geography and the natural world
  'Russia spans 11 time zones, more than any other country.',
  'Antarctica is the largest desert on Earth, since deserts are defined by how little they receive, not by heat.',
  'The Pacific Ocean covers more area than all of Earth’s land combined.',
  'Canada holds more lake water than the rest of the world’s countries put together.',
  'The Dead Sea is so salty that swimmers float on its surface without trying.',
  'Africa is the only continent that lies in all four hemispheres.',
  'Mount Everest grows a few millimetres taller each year as tectonic plates collide.',
  'The Mariana Trench is deeper than Mount Everest is tall.',
  'At Point Nemo, the ocean spot farthest from land, the nearest people are often astronauts on the space station.',
];

/** Pick a fact different from the one currently shown, when possible. */
export function pickFact(exclude?: string): string {
  if (BUDDY_FACTS.length <= 1) return BUDDY_FACTS[0] ?? '';
  let f = exclude;
  while (f === exclude) f = BUDDY_FACTS[Math.floor(Math.random() * BUDDY_FACTS.length)];
  return f ?? BUDDY_FACTS[0];
}
